"""Translation stage of a job: applies the reviewer's answers, plans and translates every segment, and persists
the results batch by batch so an interrupted run resumes where it stopped."""

import json
import logging
import os
from typing import Any, Dict, List

from ..lakebase import get_pool
from .audit import determine_mode as _determine_mode
from .jobs_db import get_glossary_rows, heartbeat_loop, update_job
from .langdetect import normalize_hyphens
from .runtime import ACTIVE_JOBS_SEMAPHORE, get_llm_credentials, spawn
from .segment_plan import length_anomaly, parse_lang_answer, plan_segment_translation
from .translate_batches import translate_unique_strings

logger = logging.getLogger(__name__)


async def apply_answers(job_id: int) -> None:
    """Apply the reviewer's answers that change segment language before translation.

    :param job_id: translation_jobs.id.
    """
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        q_rows = await conn.fetch(
            '''
            SELECT q_id, category, seg_ids, context, answer
            FROM translation_questions
            WHERE job_id = $1 AND answer IS NOT NULL AND answer != ''
            ''',
            job_id,
        )
        for q in q_rows:
            if q['category'] == 'unknown_lang':
                lang = parse_lang_answer(q['answer'])
                seg_ids = json.loads(q['seg_ids'] or '[]')
                if lang and seg_ids:
                    await conn.execute(
                        '''
                        UPDATE translation_segments
                        SET detected_lang = $3, lang_confidence = 1.0
                        WHERE job_id = $1 AND seg_id = ANY($2::text[])
                        ''',
                        job_id, seg_ids, lang,
                    )
                    logger.info('translate job %s: %s → detected_lang=%s for %d segment(s)',
                                job_id, q['q_id'], lang, len(seg_ids))


_OWN_FLAG_SQL = (
    "COALESCE(conflict_detail, '') LIKE 'Translation failed%' "
    "OR COALESCE(conflict_detail, '') LIKE 'Translation looks%' "
    "OR COALESCE(conflict_detail, '') LIKE 'DNT token(s) lost%'"
)


async def apply_resolved_segment(
    conn, seg_row: Dict[str, Any], plan: Dict[str, Any], translated_span: str,
    by_source: Dict[str, Dict[str, List[int]]], source_lang: str,
) -> None:
    """Compose, check and store one resolved translation on its segment row.

    :param conn: open database connection, in the caller's transaction.
    :param seg_row: segment row with id, source_text and dnt_tokens.
    :param plan: result of plan_segment_translation for this segment.
    :param translated_span: translation received for plan['query'].
    :param by_source: accumulator source_text -> translated text -> segment ids, updated in place.
    :param source_lang: job source language code, stored as the segment's detected language.
    """
    translated = plan['compose'](translated_span)
    # DNT post-check: part numbers / standards / codes must survive
    # translation verbatim — flag for review if not. kept_text covers the
    # side of a format-split segment that stays in its own runs (not in
    # translated_text).
    try:
        tokens = json.loads(seg_row['dnt_tokens'] or '[]')
    except (TypeError, json.JSONDecodeError):
        tokens = []
    translated_norm, kept_norm = normalize_hyphens(translated), normalize_hyphens(plan['kept_text'])
    lost_tokens = [t for t in tokens if t not in translated_norm and t not in kept_norm]
    dnt_lost = bool(lost_tokens)
    # generate_review_comments needs both conflict_flag and conflict_detail to emit a Word comment, so a lost
    # token sets a detail too; COALESCE keeps any detail audit.py already set.
    dnt_detail = f"DNT token(s) lost in translation: {', '.join(lost_tokens)}" if dnt_lost else None
    length_detail = length_anomaly(plan['query'], translated_span)
    dnt_detail = dnt_detail or length_detail
    dnt_lost = dnt_lost or bool(length_detail)
    # A segment reaching this point was, by definition, planned for
    # translation — i.e. its true language is the document's source
    # language, whatever the audit's (possibly wrong) per-segment guess had
    # said. Correct detected_lang here too, or the Lang column keeps
    # advertising a stale/wrong detection on an otherwise well-translated row.
    await conn.execute(
        # Flags set by this function or the failure path describe the segment's earlier translation, so they
        # are replaced.
        'UPDATE translation_segments SET translated_text = $2, '
        f"conflict_flag = (conflict_flag AND NOT ({_OWN_FLAG_SQL})) OR $3, "
        f"conflict_detail = CASE WHEN {_OWN_FLAG_SQL} THEN $5 "
        'ELSE COALESCE(conflict_detail, $5) END, '
        'inline_split = COALESCE($4, inline_split), '
        'detected_lang = $6, lang_confidence = NULL WHERE id = $1',
        seg_row['id'], translated, dnt_lost, plan['inline_json'], dnt_detail, source_lang,
    )
    by_source.setdefault(seg_row['source_text'], {}).setdefault(translated, []).append(seg_row['id'])


async def run_translation_stage(job_id: int, source_lang: str, target_lang: str) -> None:
    """Translate every planned segment of a job and persist the results batch by batch.

    :param job_id: translation_jobs.id, already claimed in status 'translating'.
    :param source_lang: source language code.
    :param target_lang: target language code.
    """
    pool = get_pool()
    # Held only while this stage actively runs; released on completion or failure below.
    await ACTIVE_JOBS_SEMAPHORE.acquire()
    beat = spawn(heartbeat_loop(job_id))
    try:
        endpoint = os.getenv('TRANSLATE_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''))
        if not endpoint:
            raise RuntimeError('TRANSLATE_ENDPOINT (or COMPARE_ANALYSIS_ENDPOINT) not configured')
        host, token = get_llm_credentials()
        if not host or not token:
            raise RuntimeError('Missing Databricks credentials for translation endpoint')

        # Reviewer answers can change detected_lang / DNT rules — apply them
        # before deciding what gets translated.
        await apply_answers(job_id)

        async with pool.acquire() as conn:
            seg_rows = await conn.fetch(
                'SELECT id, source_text, pattern_type, detected_lang, lang_confidence, '
                'dnt_tokens, inline_split, translated_text, out_of_page_range '
                'FROM translation_segments WHERE job_id = $1 ORDER BY id',
                job_id,
            )

        # Recompute the document mode from the persisted per-segment detections
        # (robust to stage_progress being overwritten by later stages).
        mode = _determine_mode([dict(r) for r in seg_rows], source_lang, target_lang)

        seg_row_by_id: Dict[int, Dict[str, Any]] = {}
        plans: Dict[int, Dict[str, Any]] = {}
        for r in seg_rows:
            row = dict(r)
            try:
                row['inline_split'] = json.loads(r['inline_split']) if r['inline_split'] else None
            except (TypeError, json.JSONDecodeError):
                row['inline_split'] = None
            seg_row_by_id[r['id']] = row
            # Already resolved by a prior (crashed/retried) run of this stage —
            # skip re-translating it. This is what makes the stage resumable:
            # translated_text is written per-batch below, not only at the end.
            if row['translated_text'] is not None:
                continue
            plan = plan_segment_translation(row, source_lang, target_lang, mode)
            if plan:
                plans[r['id']] = plan

        async with pool.acquire() as conn:
            # Everything NOT planned for translation (kept-side language,
            # DNT, numeric-only, bilingual-inline rows with no source-language
            # side) keeps its source text untouched at rebuild. Without this
            # flag those rows carry translated_text=NULL and crash the
            # fit-check stage. Restricted to still-unresolved rows so a resumed
            # run doesn't flip an already-translated row's keep_as_is back on.
            await conn.execute(
                '''
                UPDATE translation_segments
                SET keep_as_is = NOT (id = ANY($2::bigint[]))
                WHERE job_id = $1 AND translated_text IS NULL
                ''',
                job_id, list(plans.keys()),
            )
        # First-occurrence order (not sorted): keeps neighbouring segments in
        # the same LLM batch, which helps terminology consistency.
        unique_strings = list(dict.fromkeys(p['query'] for p in plans.values()))
        query_to_seg_ids: Dict[str, List[int]] = {}
        for seg_id, plan in plans.items():
            query_to_seg_ids.setdefault(plan['query'], []).append(seg_id)

        already_resolved = sum(1 for r in seg_row_by_id.values() if r['translated_text'] is not None)
        await update_job(
            job_id, status='translating',
            stage_progress={
                'stage': 'translating',
                'done': already_resolved,
                'total': len(unique_strings) + already_resolved,
            },
        )

        # Tracks (source_text -> {translated_text -> [seg_id, ...]}) for the
        # post-loop consistency check below — two segments with the exact
        # same source_text should never end up with a different translation.
        by_source: Dict[str, Dict[str, List[int]]] = {}

        async def _on_batch_resolved(resolved: Dict[str, str]) -> None:
            """Persist one batch of resolved translations in its own transaction.

            :param resolved: source string -> translation for the batch.
            """
            # Committed per batch (own short transaction) rather than waiting
            # for the whole job to finish translating — a crash/restart after
            # this point keeps this batch's work instead of losing it (see
            # the translated_text IS NULL resume filter above).
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for query, translated_span in resolved.items():
                        for seg_id in query_to_seg_ids.get(query, []):
                            await apply_resolved_segment(
                                conn, seg_row_by_id[seg_id], plans[seg_id], translated_span, by_source, source_lang,
                            )

        if unique_strings:
            glossary_rows = await get_glossary_rows()
            translations, failed = await translate_unique_strings(
                host, token, endpoint, unique_strings, source_lang, target_lang, glossary_rows, job_id,
                on_resolved=_on_batch_resolved,
                progress_offset=already_resolved, progress_total=already_resolved + len(unique_strings),
            )
        else:
            translations, failed = {}, []

        if failed:
            # Unresolved after every retry: keep the source text at rebuild
            # and flag for manual review.
            failed_seg_ids = [seg_id for seg_id, plan in plans.items() if plan['query'] in failed]
            async with pool.acquire() as conn:
                await conn.execute(
                    'UPDATE translation_segments SET conflict_flag = TRUE, keep_as_is = TRUE, '
                    "conflict_detail = COALESCE(conflict_detail, 'Translation failed after all retries — use the translate button') "
                    'WHERE id = ANY($1::bigint[])',
                    failed_seg_ids,
                )

        # Deterministic consistency guard: the same exact source_text should
        # never resolve to two different translations within one job (can
        # happen across separately-composed inline splits even though the
        # underlying `query` was deduped). Flag for review, never auto-fix.
        inconsistent_ids = [
            seg_id
            for variants in by_source.values() if len(variants) > 1
            for seg_id in [i for ids in variants.values() for i in ids]
        ]
        if inconsistent_ids:
            async with pool.acquire() as conn:
                await conn.execute(
                    '''
                    UPDATE translation_segments
                    SET conflict_flag = TRUE,
                        conflict_detail = COALESCE(conflict_detail, 'Same source text translated differently elsewhere in this document')
                    WHERE id = ANY($1::bigint[])
                    ''',
                    inconsistent_ids,
                )
            logger.warning('translate job %s: %d segment(s) flagged for inconsistent terminology',
                            job_id, len(inconsistent_ids))

        await update_job(
            job_id, status='translated',
            stage_progress={
                'stage': 'translated',
                'mode': mode,
                'unique_strings': len(unique_strings),
                'translated_count': len(translations),
                'failed_count': len(failed),
            },
        )
        if failed:
            logger.warning('translate job %s: %d/%d unique strings failed translation after retries',
                            job_id, len(failed), len(unique_strings))
        logger.info('translate job %s: translation stage complete (%d/%d unique strings)',
                    job_id, len(translations), len(unique_strings))
    except Exception as e:
        logger.error('translate job %s: translation stage failed: %s', job_id, e, exc_info=True)
        await update_job(job_id, status='failed', error_type=type(e).__name__, error_msg=str(e)[:2000])
    finally:
        beat.cancel()
        ACTIVE_JOBS_SEMAPHORE.release()
