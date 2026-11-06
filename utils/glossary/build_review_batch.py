"""Boucle d'industrialisation glossaire — familles multilingues → CSV de revue.

Enchaîne sur toutes les familles multilingues disposant de la paire de langues
demandée : alignement des chunks, extraction LLM des termes, consolidation
multi-sources (un terme vu dans N familles = confiance N), déduplication
contre le glossaire existant (Translator/glossary/terms.csv, seedé en table),
puis un unique CSV de revue trié par confiance.

    python -m utils.glossary.build_review_batch --langs fr en --max-families 60
    python -m utils.glossary.build_review_batch --langs fr en --define-top 100

Coût maîtrisé : gpt-5-4-mini, --max-sections par famille, extraction en
parallèle (threads). status=pending — jamais d'insertion directe.
"""

import argparse
import csv
import json
import logging
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .extract_glossary_candidates import align, fetch_chunks, llm_define, llm_extract
from .find_multilingual_pairs import find_families
from ._sql import LANG_SUFFIX_TO_CODE

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TERMS_CSV = REPO_ROOT / 'Translator' / 'glossary' / 'terms.csv'

logger = logging.getLogger(__name__)


def _norm(term: str) -> str:
    """Normalise a term for comparison.

    :param term: Raw term.
    :return: Lower-cased, stripped term without diacritics.
    """
    t = unicodedata.normalize('NFKD', term.lower().strip())
    return ''.join(c for c in t if not unicodedata.combining(c))


def existing_terms(lang_a: str, lang_b: str) -> set:
    """Collect the terms already present in the current glossary.

    :param lang_a: First language code (CSV column).
    :param lang_b: Second language code (CSV column).
    :return: Normalised terms (see ``_norm``) of both languages; empty when the CSV does not exist.
    """
    if not TERMS_CSV.exists():
        return set()
    seen = set()
    with TERMS_CSV.open('r', encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            for lang in (lang_a, lang_b):
                v = (row.get(lang) or '').strip()
                if v:
                    seen.add(_norm(v))
    return seen


def family_refs(fam: dict, lang_a: str, lang_b: str) -> 'tuple[str, str] | None':
    """Pick the document reference of each language in a family.

    :param fam: Family dict as returned by ``find_families``.
    :param lang_a: First language code.
    :param lang_b: Second language code.
    :return: ``(ref in lang_a, ref in lang_b)``, or None when either language is missing.
    """
    by_code = {}
    for v in fam['versions']:
        code = LANG_SUFFIX_TO_CODE.get(v['suffix'])
        if code and code not in by_code:
            by_code[code] = v['ref']
    if lang_a in by_code and lang_b in by_code:
        return by_code[lang_a], by_code[lang_b]
    return None


def process_family(fam: dict, lang_a: str, lang_b: str, endpoint: str,
                   host: str, token: str, max_sections: int) -> tuple[list[dict], bool]:
    """Extract the term pairs of one family through the LLM.

    :param fam: Family dict as returned by ``find_families``.
    :param lang_a: First language code.
    :param lang_b: Second language code.
    :param endpoint: Name of the serving endpoint used for extraction.
    :param host: Workspace URL.
    :param token: Bearer token for the workspace.
    :param max_sections: Maximum number of aligned sections sent to the LLM.
    :return: ``(candidates, ok)``; candidates carry both terms and ``source``; ``ok`` is False when extraction
        errored (rate limit, network) so the caller can retry the family.
    """
    refs = family_refs(fam, lang_a, lang_b)
    if not refs:
        return [], True
    ref_a, ref_b = refs
    try:
        pairs = align(fetch_chunks(ref_a), fetch_chunks(ref_b))
        strong = sorted((p for p in pairs if p['score'] > 0.3), key=lambda x: -x['score'])[:max_sections]
        out = []
        for p in strong:
            for t in llm_extract(p, lang_a, lang_b, endpoint, host, token):
                # The language-independent family id, so 'sources' names the document, not one language version.
                out.append({lang_a: t[lang_a].strip(), lang_b: t[lang_b].strip(),
                            'source': fam['base']})
        return out, True
    except Exception as e:
        logger.warning('  ! %s: %s', fam['base'], e)
        return [], False


def main() -> None:
    """Command-line entry point: build the review CSV of new glossary candidates for one language pair."""
    # A cp1252 Windows console cannot print Czech/Bulgarian terms and would crash after the CSV is written.
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--langs', nargs=2, default=['fr', 'en'])
    ap.add_argument('--endpoint', default='databricks-gpt-5-4-mini')
    ap.add_argument('--max-families', type=int, default=1000)
    ap.add_argument('--max-sections', type=int, default=3, help='sections LLM par famille')
    ap.add_argument('--define-top', type=int, default=0, help='générer une définition pour les N premiers')
    # 6 workers stay under the Databricks input/output token-per-minute limits.
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--out', type=Path,
                    default=REPO_ROOT / 'Translator' / 'glossary' / 'candidates' / 'review_batch.csv',
                    help='défaut: Translator/glossary/candidates/review_batch_<a>-<b>.csv')
    args = ap.parse_args()
    lang_a, lang_b = args.langs
    out_path = args.out
    if out_path.name == 'review_batch.csv':  # default: suffix with the pair
        out_path = out_path.with_name(f'review_batch_{lang_a}-{lang_b}.csv')

    host = 'https://dbc-3a17bfce-9e88.cloud.databricks.com'
    token = json.loads(subprocess.check_output(
        ['databricks', 'auth', 'token', '-p', 'UAT'], text=True, encoding='utf-8'))['access_token']

    families = [f for f in find_families() if family_refs(f, lang_a, lang_b)][:args.max_families]
    logger.info('%d familles avec la paire %s/%s — extraction (%d threads)…', len(families), lang_a, lang_b, args.workers)

    consolidated: dict = {}

    def _collect(candidates: list[dict]) -> None:
        """Merge extracted candidates into ``consolidated``, one entry per normalised pair.

        :param candidates: Term dicts with both language values and ``source``.
        """
        for c in candidates:
            key = (_norm(c[lang_a]), _norm(c[lang_b]))
            if not key[0] or not key[1]:
                continue
            entry = consolidated.setdefault(key, {**c, 'sources': set()})
            entry['sources'].add(c['source'])

    failed: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_family, f, lang_a, lang_b, args.endpoint, host, token,
                          args.max_sections): f for f in families}
        for fut in as_completed(futs):
            done += 1
            candidates, ok = fut.result()
            _collect(candidates)
            if not ok:
                failed.append(futs[fut])
            if done % 20 == 0:
                logger.info('  %d/%d familles — %d termes uniques', done, len(families), len(consolidated))

    if failed:
        # One call at a time cannot hit the token-per-minute limits, so remaining failures are real.
        logger.info('%d famille(s) en échec — rattrapage séquentiel…', len(failed))
        still_failed = []
        for f in failed:
            candidates, ok = process_family(f, lang_a, lang_b, args.endpoint, host, token,
                                            args.max_sections)
            _collect(candidates)
            if not ok:
                still_failed.append(f['base'])
        if still_failed:
            logger.warning('  !! toujours en échec (à relancer) : %s', ', '.join(still_failed))
        else:
            logger.info('  rattrapage complet — aucune famille perdue')

    known = existing_terms(lang_a, lang_b)
    rows = [e for k, e in consolidated.items() if k[0] not in known and k[1] not in known]
    rows.sort(key=lambda e: (-len(e['sources']), e[lang_a].lower()))
    logger.info('%d termes uniques, %d déjà au glossaire, %d nouveaux candidats',
                len(consolidated), len(consolidated) - len(rows), len(rows))

    if args.define_top > 0 and rows:
        top = rows[:args.define_top]
        logger.info('génération des définitions pour les %d premiers…', len(top))
        llm_define(top, lang_a, lang_b, args.endpoint, host, token)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f, delimiter=';')
        w.writerow([lang_a, lang_b, 'n_docs', 'sources', 'definition', 'definition_source', 'status'])
        for e in rows:
            w.writerow([e[lang_a], e[lang_b], len(e['sources']), ', '.join(sorted(e['sources'])[:4]),
                        e.get('definition', ''), e.get('definition_source', ''), 'pending'])
    logger.info('-> %s', out_path)
    for e in rows[:15]:
        logger.info('  [%d docs] %-38s -> %s', len(e['sources']), e[lang_a], e[lang_b])


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
