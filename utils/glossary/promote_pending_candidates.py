"""LLM-verify the backlog of `glossary_candidates` (status='pending') and promote survivors
straight into `glossary_terms`.

It clears the backlog of the Intraqual-corpus bulk import with the same llm_verify() check as
sync_candidates_to_lakebase.py (reject headings/fragments/mismatches, normalize stray ALL-CAPS casing).
Terms coming from real translation work go through human review instead.

Kept candidates -> inserted into glossary_terms, candidate row marked
status='approved' (reviewed_by='llm-auto-verify'). Rejected candidates ->
status='rejected' with a reject_reason, left in glossary_candidates for audit
(not deleted).

Dry-run by default (just counts per language pair) — pass --apply to verify
and write for real.

    python -m utils.glossary.promote_pending_candidates --env UAT-TEST
    python -m utils.glossary.promote_pending_candidates --env UAT-TEST --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / 'utils' / 'databricks_ops' / 'lakebase_sync'))

from .build_review_batch import _norm
from .sync_candidates_to_lakebase import LANGS, _databricks_auth, llm_verify

logger = logging.getLogger(__name__)


def main() -> None:
    """Command-line entry point: verify pending candidates and, with ``--apply``, publish them to the glossary."""
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--env', choices=['DEV', 'UAT', 'UAT-TEST'], default='UAT-TEST',
                    help='Environnement Lakebase cible (défaut : UAT-TEST, la base jetable)')
    ap.add_argument('--apply', action='store_true', help='vérifier et écrire réellement (défaut : dry-run)')
    ap.add_argument('--endpoint', default=os.getenv('GLOSSARY_EXTRACT_ENDPOINT', 'databricks-gpt-5-4-mini'))
    ap.add_argument('--status', default='pending', help='statut des candidats à traiter (défaut : pending)')
    args = ap.parse_args()

    from migrate_lakebase import sdk_lakebase_connect
    logger.info('Connexion Lakebase %s…', args.env)
    conn = sdk_lakebase_connect(args.env)
    try:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT id, en, fr, cs, bg, de, es, definition, definition_source '
                'FROM glossary_candidates WHERE status = %s ORDER BY id',
                (args.status,),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        logger.info("%d candidat(s) au statut '%s' dans glossary_candidates.", len(rows), args.status)
        if not rows:
            return

        by_pair: dict[tuple, list[dict]] = {}
        for r in rows:
            present = tuple(sorted(l for l in LANGS if r.get(l)))
            by_pair.setdefault(present, []).append(r)
        for present, group in by_pair.items():
            label = '/'.join(present) if len(present) == 2 else str(present)
            logger.info('  %s: %d candidat(s)', label, len(group))

        if not args.apply:
            logger.info('\nDRY-RUN — rien vérifié ni écrit. Relancer avec --apply pour publier.')
            return

        endpoint, host, token = _databricks_auth(args.endpoint)

        with conn.cursor() as cur:
            cur.execute('SELECT en, fr, cs, bg, de, es FROM glossary_terms')
            known = {_norm(v) for row in cur.fetchall() for v in row if v and v.strip()}
            cur.execute(
                r"SELECT COALESCE(MAX(SUBSTRING(term_id FROM 2)::int), 0) FROM glossary_terms WHERE term_id ~ '^T\d+$'"
            )
            next_id = (cur.fetchone()[0] or 0) + 1

        n_kept = n_rejected_verify = n_rejected_dup = n_skipped_pair = 0
        for present, group in by_pair.items():
            if len(present) != 2:
                logger.warning('  ! %d ligne(s) avec %d langue(s) (%s) — ignorées (paire attendue)',
                               len(group), len(present), present)
                n_skipped_pair += len(group)
                continue
            lang_a, lang_b = present
            logger.info('Vérification LLM %s/%s : %d candidat(s)…', lang_a, lang_b, len(group))
            kept = llm_verify(group, lang_a, lang_b, endpoint, host, token)
            kept_ids = {r['id'] for r in kept}
            n_rejected_verify += sum(1 for r in group if r['id'] not in kept_ids)
            logger.info('  -> %d/%d gardé(s) après vérification', len(kept), len(group))

            with conn.cursor() as cur:
                for r in group:
                    if r['id'] in kept_ids:
                        continue
                    cur.execute(
                        "UPDATE glossary_candidates SET status = 'rejected', reviewed_by = %s, "
                        "reviewed_at = NOW(), reject_reason = %s WHERE id = %s",
                        ('llm-auto-verify', 'Automated verification: heading/fragment/mismatch', r['id']),
                    )
                for r in kept:
                    keys = {_norm(r[l]) for l in LANGS if r.get(l)}
                    if keys & known:
                        cur.execute(
                            "UPDATE glossary_candidates SET status = 'rejected', reviewed_by = %s, "
                            "reviewed_at = NOW(), reject_reason = %s WHERE id = %s",
                            ('llm-auto-verify', 'Duplicate of an existing glossary term', r['id']),
                        )
                        n_rejected_dup += 1
                        continue
                    known |= keys
                    term_id = f'T{next_id:04d}'
                    next_id += 1
                    cur.execute(
                        '''
                        INSERT INTO glossary_terms (term_id, en, fr, cs, bg, de, es, definition, definition_source)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ''',
                        (term_id, *[r.get(l, '') for l in LANGS], r.get('definition', ''), r.get('definition_source', '')),
                    )
                    cur.execute(
                        "UPDATE glossary_candidates SET status = 'approved', reviewed_by = %s, reviewed_at = NOW() "
                        "WHERE id = %s",
                        ('llm-auto-verify', r['id']),
                    )
                    n_kept += 1
        conn.commit()
        logger.info('\nPublié %d terme(s) dans glossary_terms.', n_kept)
        logger.info('Rejetés : %d (vérification LLM), %d (doublon), %d ignorés (paire de langues inattendue).',
                    n_rejected_verify, n_rejected_dup, n_skipped_pair)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
