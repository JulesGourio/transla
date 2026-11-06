"""Publication des candidats glossaire -> `glossary_terms` (Lakebase), avec
verif LLM au lieu d'une revue humaine.

Ces paires de termes viennent de traductions professionnelles reelles (deux
versions officielles du meme document LATECOERE, alignees section par
section) : on part du principe que ce sont deja de bonnes traductions, pas
des candidats a valider un par un a la main. Le seul risque vient de
l'extraction elle-meme (alignement de chunk approximatif, LLM d'extraction
qui se trompe de paire, ou qui garde la casse ALL-CAPS d'un titre de section
comme si c'etait un terme) - d'ou une passe de verification LLM juste avant
publication, qui rejette les artefacts (titres/fragments de phrase/paires
non correspondantes) et corrige la casse (minuscule pour un nom commun,
casse d'origine gardee seulement pour les noms propres/acronymes).

1. lit un ou plusieurs CSV de candidats (delimiteur ';', memes colonnes que
   `review_shortlist.csv` : priority/pair/fr/en/cs/bg/de/es/n_docs/
   definition/definition_source/sources/status - defaut : review_shortlist.csv),
2. dedoublonne (meme normalisation `_norm` que build_review_batch.py) contre
   ce qui est deja dans `glossary_terms` en Lakebase (le glossaire vivant,
   pas Translator/glossary/terms.csv),
3. verifie chaque paire restante via LLM (par lot, groupe par paire de
   langues) - normalise la casse, rejette ce qui n'est pas un terme
   autonome valide,
4. insere directement dans `glossary_terms` (term_id auto T####) ce qui
   passe la verification. `glossary_candidates` (revue humaine, ajout manuel
   depuis le panel) n'est pas alimentee par ce script.

Par defaut en DRY-RUN (pas d'appel LLM, juste le compte apres dedup) -
passer --apply pour verifier et ecrire reellement.

    python -m utils.glossary.sync_candidates_to_lakebase --env UAT-TEST
    python -m utils.glossary.sync_candidates_to_lakebase --env UAT-TEST --apply
    python -m utils.glossary.sync_candidates_to_lakebase Translator/glossary/candidates/review_batch_fr-en.csv --env UAT-TEST --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / 'utils' / 'databricks_ops' / 'lakebase_sync'))

from .build_review_batch import _norm  # same normalisation as the upstream dedup
from server.services.translation.glossary_verify import llm_verify  # noqa: F401 (re-exported for CLI usage below)

LANGS = ('en', 'fr', 'cs', 'bg', 'de', 'es')
DEFAULT_CSV = REPO_ROOT / 'Translator' / 'glossary' / 'candidates' / 'review_shortlist.csv'

logger = logging.getLogger(__name__)


def _databricks_auth(endpoint: str) -> tuple[str, str, str]:
    """Resolve the workspace credentials of the LLM endpoint.

    :param endpoint: Name of the serving endpoint, returned unchanged.
    :return: ``(endpoint, host, token)``; taken from DATABRICKS_HOST/DATABRICKS_TOKEN when both are set,
        otherwise from the UAT CLI profile.
    """
    env_host, env_token = os.environ.get('DATABRICKS_HOST'), os.environ.get('DATABRICKS_TOKEN')
    if env_host and env_token:
        return endpoint, env_host.rstrip('/'), env_token
    host = 'https://dbc-3a17bfce-9e88.cloud.databricks.com'
    token = json.loads(subprocess.check_output(
        ['databricks', 'auth', 'token', '-p', 'UAT'], text=True, encoding='utf-8'))['access_token']
    return endpoint, host, token


def load_rows(paths: list[Path]) -> list[dict]:
    """Read candidate CSVs (delimiter ';') into normalised rows.

    :param paths: CSV files with at least one language column.
    :return: One dict per row having at least one term: a value per language in ``LANGS``, ``n_docs``, ``sources``,
        ``definition``, ``definition_source``, ``priority`` (None if absent) and ``status``. Files without a language
        column are skipped.
    """
    rows: list[dict] = []
    for path in paths:
        with path.open('r', encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f, delimiter=';')
            fields = [c.strip().lower() for c in (reader.fieldnames or [])]
            lang_cols = [c for c in fields if c in LANGS]
            if not lang_cols:
                logger.warning('  %s: aucune colonne de langue (%s) — ignoré', path.name, fields)
                continue
            n = 0
            for raw in reader:
                row = {(k or '').strip().lower(): (v or '').strip() for k, v in raw.items()}
                terms = {c: row[c] for c in lang_cols if row.get(c)}
                if not terms:
                    continue
                n += 1
                try:
                    priority = int(row['priority']) if row.get('priority') else None
                except ValueError:
                    priority = None
                try:
                    n_docs = int(row.get('n_docs') or 0)
                except ValueError:
                    n_docs = 0
                rows.append({
                    **{c: terms.get(c, '') for c in LANGS},
                    'n_docs': n_docs,
                    'sources': row.get('sources') or row.get('source') or '',
                    'definition': row.get('definition', ''),
                    'definition_source': row.get('definition_source', ''),
                    'priority': priority,
                    'status': (row.get('status') or 'pending').lower() or 'pending',
                })
            logger.info('  %s: %d ligne(s) avec au moins un terme', path.name, n)
    return rows


def main() -> None:
    """Command-line entry point: dedupe, LLM-verify and publish candidate terms to ``glossary_terms``."""
    # A cp1252 Windows console cannot print arrows or em dashes of the help text and summary.
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('csvs', nargs='*', type=Path, default=[DEFAULT_CSV],
                    help=f'CSV de candidats (défaut : {DEFAULT_CSV.relative_to(REPO_ROOT)})')
    ap.add_argument('--env', choices=['DEV', 'UAT', 'UAT-TEST'], default='UAT-TEST',
                    help='Environnement Lakebase cible (défaut : UAT-TEST, la base jetable)')
    ap.add_argument('--apply', action='store_true', help="écrire réellement (défaut : dry-run)")
    ap.add_argument('--endpoint', default=os.getenv('GLOSSARY_EXTRACT_ENDPOINT', 'databricks-gpt-5-4-mini'))
    ap.add_argument('--no-llm-verify', action='store_true',
                    help='publier sans passer par la verification LLM (deconseille - depannage seulement)')
    args = ap.parse_args()

    rows = load_rows(args.csvs)
    if not rows:
        logger.info('Rien à publier.')
        return

    from migrate_lakebase import sdk_lakebase_connect
    logger.info('Connexion Lakebase %s…', args.env)
    conn = sdk_lakebase_connect(args.env)
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT en, fr, cs, bg, de, es FROM glossary_terms')
            known = {_norm(v) for row in cur.fetchall() for v in row if v and v.strip()}
            # Rows already queued for review must not be republished as new terms.
            cur.execute('SELECT en, fr, cs, bg, de, es FROM glossary_candidates')
            known |= {_norm(v) for row in cur.fetchall() for v in row if v and v.strip()}

        to_insert, skipped = [], 0
        for r in rows:
            if any(_norm(r[l]) in known for l in LANGS if r[l]):
                skipped += 1
                continue
            for l in LANGS:
                if r[l]:
                    known.add(_norm(r[l]))
            to_insert.append(r)

        logger.info('%d lignes lues, %d déjà au glossaire ou en attente de revue, %d à publier',
                    len(rows), skipped, len(to_insert))
        for r in to_insert[:15]:
            langs = ' | '.join(f'{l}={r[l]}' for l in LANGS if r[l])
            logger.info('  [P%s n_docs=%s] %s', r['priority'], r['n_docs'], langs)
        if len(to_insert) > 15:
            logger.info('  … et %d de plus', len(to_insert) - 15)

        if not args.apply:
            logger.info('\nDRY-RUN — rien vérifié ni écrit. Relancer avec --apply pour publier.')
            return

        if args.no_llm_verify:
            logger.info('--no-llm-verify : aucune vérification, publication telle quelle.')
            verified = to_insert
        else:
            by_pair: dict[tuple, list[dict]] = {}
            for r in to_insert:
                present = tuple(sorted(l for l in LANGS if r[l]))
                by_pair.setdefault(present, []).append(r)
            endpoint, host, token = _databricks_auth(args.endpoint)
            verified = []
            for present, group in by_pair.items():
                if len(present) != 2:
                    logger.warning('  ! %d ligne(s) avec %d langue(s) (%s) — gardées sans vérification (paire attendue)',
                                   len(group), len(present), present)
                    verified.extend(group)
                    continue
                lang_a, lang_b = present
                logger.info('Vérification LLM %s/%s : %d candidat(s)…', lang_a, lang_b, len(group))
                ok = llm_verify(group, lang_a, lang_b, endpoint, host, token)
                logger.info('  -> %d/%d gardé(s) après vérification', len(ok), len(group))
                verified.extend(ok)

            # Case normalisation can make two rows of the batch converge on the same term.
            seen_now: set = set()
            deduped = []
            for r in verified:
                keys = {_norm(r[l]) for l in LANGS if r[l]}
                if keys & seen_now:
                    continue
                seen_now |= keys
                deduped.append(r)
            verified = deduped

        with conn.cursor() as cur:
            cur.execute(
                r"SELECT COALESCE(MAX(SUBSTRING(term_id FROM 2)::int), 0) FROM glossary_terms WHERE term_id ~ '^T\d+$'"
            )
            next_id = (cur.fetchone()[0] or 0) + 1
            for r in verified:
                term_id = f'T{next_id:04d}'
                next_id += 1
                cur.execute(
                    '''
                    INSERT INTO glossary_terms (term_id, en, fr, cs, bg, de, es, definition, definition_source)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''',
                    (term_id, *[r[l] for l in LANGS], r['definition'], r['definition_source']),
                )
        conn.commit()
        logger.info('Publié %d terme(s) dans glossary_terms (%s).', len(verified), args.env)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
