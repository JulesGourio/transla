"""Prototype glossaire #1 — inventaire des familles de documents multilingues.

Le corpus indexé (uat_landingzone.qualibot.chunks_v2) contient des documents
publiés en plusieurs langues sous la même référence avec un suffixe de langue
(IF20335_FR / IF20335_GB / IF20335_BG).
Ces paires sont des traductions professionnelles alignées : la matière première
idéale pour peupler le glossaire Translate avec vérification humaine.

    python -m utils.glossary.find_multilingual_pairs --out familles.json

Sortie : une entrée par famille {base, langs, refs, divisions, chunk_counts}.
Coût : une requête SQL sur le warehouse serverless (aucun LLM).
"""

import argparse
import json
import logging
from pathlib import Path

from ._sql import CHUNKS_TABLE, LANG_SUFFIX_TO_CODE, SUFFIX_RE, run_sql

logger = logging.getLogger(__name__)


def find_families() -> list[dict]:
    """List the document families published in several languages.

    :return: One dict per family with ``base`` (reference without language suffix), ``langs`` (sorted language codes),
        ``divisions`` (sorted) and ``versions`` (dicts with ref, suffix, division, n_chunks).
    """
    rows = run_sql(f"""
        WITH t AS (
          SELECT REF, division,
                 regexp_replace(REF, '{SUFFIX_RE}', '') AS base,
                 upper(regexp_extract(REF, '{SUFFIX_RE}', 1)) AS suffix,
                 COUNT(*) AS n_chunks
          FROM {CHUNKS_TABLE}
          GROUP BY REF, division
        )
        SELECT base,
               collect_list(named_struct('ref', REF, 'suffix', suffix,
                                         'division', division, 'n_chunks', n_chunks)) AS versions
        FROM t
        WHERE suffix != ''
        GROUP BY base
        HAVING size(collect_set(suffix)) >= 2
        ORDER BY base
    """)
    families = []
    for base, versions_json in rows:
        versions = json.loads(versions_json) if isinstance(versions_json, str) else versions_json
        langs = sorted({LANG_SUFFIX_TO_CODE.get(v['suffix'], v['suffix'].lower()) for v in versions})
        families.append({
            'base': base,
            'langs': langs,
            'divisions': sorted({v['division'] for v in versions}),
            'versions': versions,
        })
    return families


def main() -> None:
    """Command-line entry point: print the richest families and optionally write the full JSON inventory."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, help='write full JSON inventory here')
    args = ap.parse_args()

    families = find_families()
    n_langs = {}
    for f in families:
        n_langs[len(f['langs'])] = n_langs.get(len(f['langs']), 0) + 1
    logger.info('%d familles multilingues (%s par nombre de langues)', len(families), dict(sorted(n_langs.items())))

    # Les familles les plus riches d'abord — meilleures candidates au glossaire
    top = sorted(families, key=lambda f: -len(f['langs']))[:10]
    for f in top:
        total = sum(v['n_chunks'] for v in f['versions'])
        logger.info('  %-30s %-14s %d versions, %d chunks', f['base'], '/'.join(f['langs']), len(f['versions']), total)

    if args.out:
        args.out.write_text(json.dumps(families, ensure_ascii=False, indent=1), encoding='utf-8')
        logger.info('-> inventaire complet : %s', args.out)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
