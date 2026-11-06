"""Glossary candidates proposed from the confirmed translations of a validated job."""

import asyncio
import logging
import os

from ..lakebase import get_pool
from .glossary_extract import extract_job_candidates, normalize_term
from .glossary_verify import llm_verify
from .runtime import get_llm_credentials
from .translate_batches import LANG_NAMES

logger = logging.getLogger(__name__)


async def propose_glossary_candidates(
    job_id: int, source_lang: str, target_lang: str,
) -> None:
    """Propose glossary candidates from a validated job's translations; best effort, never raises.

    :param job_id: translation_jobs.id.
    :param source_lang: source language code, a key of LANG_NAMES.
    :param target_lang: target language code, a key of LANG_NAMES.
    """
    try:
        pool = get_pool()
        if not pool:
            return
        async with pool.acquire() as conn:
            seg_rows = await conn.fetch(
                '''
                SELECT source_text, translated_text FROM translation_segments
                WHERE job_id = $1 AND pattern_type = 'mono' AND NOT keep_as_is
                ''',
                job_id,
            )
        pairs = [
            (r['source_text'], r['translated_text'])
            for r in seg_rows
            if r['source_text'] and r['translated_text'] and r['source_text'] != r['translated_text']
        ]
        if not pairs:
            return
        endpoint = os.getenv('TRANSLATE_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''))
        host, token = get_llm_credentials()
        if not endpoint or not host or not token:
            return
        if source_lang not in LANG_NAMES or target_lang not in LANG_NAMES:
            return  # extra guard: these become raw column names in the INSERT below

        candidates = await extract_job_candidates(pairs, source_lang, target_lang, host, token, endpoint)
        if not candidates:
            return

        pool = get_pool()
        if not pool:
            return
        async with pool.acquire() as conn:
            job = await conn.fetchrow('SELECT original_filename FROM translation_jobs WHERE id = $1', job_id)
            source_label = (job['original_filename'] if job else None) or f'job {job_id}'

            known = {
                normalize_term(v) for r in await conn.fetch('SELECT en, fr, cs, bg, de, es, pt, ar FROM glossary_terms')
                for v in dict(r).values() if v
            }
            # A rejected candidate stays blocked, like an approved term, so the same pair is never proposed again.
            known |= {
                normalize_term(v)
                for r in await conn.fetch(
                    "SELECT en, fr, cs, bg, de, es, pt, ar FROM glossary_candidates WHERE status = 'rejected'"
                )
                for v in dict(r).values() if v
            }
            existing_cand = [dict(r) for r in await conn.fetch(
                'SELECT id, en, fr, cs, bg, de, es, pt, ar, n_docs, sources FROM glossary_candidates'
                " WHERE status = 'pending'"
            )]

            novel = [c for c in candidates if not any(normalize_term(v) in known for v in c.values())]
            if novel:
                # LLM verification catches extraction artifacts before a candidate reaches human review; it is a
                # blocking call, so it runs off the event loop.
                novel = await asyncio.to_thread(
                    llm_verify, novel, source_lang, target_lang, endpoint, host, token,
                )

            inserted = 0
            for c in novel:
                match = next(
                    (r for r in existing_cand
                     if normalize_term(r[source_lang] or '') == normalize_term(c[source_lang])
                     and normalize_term(r[target_lang] or '') == normalize_term(c[target_lang])),
                    None,
                )
                if match:
                    sources = match['sources'] or ''
                    if source_label not in [s.strip() for s in sources.split(',')]:
                        sources = f'{sources}, {source_label}' if sources else source_label
                        await conn.execute(
                            'UPDATE glossary_candidates SET n_docs = n_docs + 1, sources = $2 WHERE id = $1',
                            match['id'], sources[:500],
                        )
                    continue
                await conn.execute(
                    f'''
                    INSERT INTO glossary_candidates ({source_lang}, {target_lang}, n_docs, sources, status)
                    VALUES ($1, $2, 1, $3, 'pending')
                    ''',
                    c[source_lang], c[target_lang], source_label,
                )
                inserted += 1
                known.add(normalize_term(c[source_lang]))
                known.add(normalize_term(c[target_lang]))
        if inserted:
            logger.info('translate job %s: proposed %d new glossary candidate(s)', job_id, inserted)
    except Exception as e:
        logger.warning('translate job %s: glossary candidate proposal failed: %s', job_id, e)
