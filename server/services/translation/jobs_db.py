"""Persistence of translation jobs, segments, questions and LLM usage in Lakebase, plus the worker heartbeat."""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from ..lakebase import get_pool
from ..llm import cost_eur

logger = logging.getLogger(__name__)


# Statuses where a worker is (or should be) actively processing — these are the
# only ones whose heartbeat going stale means the job is genuinely orphaned. The
# resting states awaiting_answers / answered / translated are user-gated: no
# worker runs while the reviewer decides, so their heartbeat naturally goes
# stale and they must SURVIVE a restart (resumable by re-polling), not be failed
# or flagged stalled. 'uploaded' is included: run_job starts right after
# create, so an 'uploaded' job with a dead worker never got picked up.
ACTIVE_PROCESSING_STATUSES = (
    'uploaded', 'extracting', 'auditing', 'translating',
    'fit_checking', 'rebuilding', 'validating',
)


HEARTBEAT_S = 30


async def heartbeat_loop(job_id: int) -> None:
    """Refresh worker_heartbeat every HEARTBEAT_S seconds until cancelled.

    :param job_id: translation_jobs.id of the job whose stage is running.
    """
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        pool = get_pool()
        if not pool:
            continue
        try:
            async with pool.acquire() as conn:
                await conn.execute('UPDATE translation_jobs SET worker_heartbeat = NOW() WHERE id = $1', job_id)
        except Exception as e:
            logger.debug('translate job %s: heartbeat failed: %s', job_id, e)


async def create_job(user_id: str, workspace_id: Optional[str], source_lang: str,
                     target_lang: str, original_filename: str,
                     selected_image_paths: Optional[List[str]] = None,
                     page_filter: Optional[str] = None) -> int:
    """Insert a translation job in status 'uploaded', owned by the calling process.

    :param user_id: identity of the uploading user.
    :param workspace_id: workspace of the user, None when unknown.
    :param source_lang: lowercase source language code.
    :param target_lang: lowercase target language code.
    :param original_filename: name of the uploaded file as sent by the client.
    :param selected_image_paths: media filenames chosen for image OCR; None or empty stores NULL.
    :param page_filter: raw page specification such as '3-10,12'; None or empty stores NULL.
    :return: id of the new job.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            '''
            INSERT INTO translation_jobs
                (user_id, workspace_id, status, source_lang, target_lang,
                 original_filename, worker_pid, worker_heartbeat, selected_image_paths, page_filter)
            VALUES ($1, $2, 'uploaded', $3, $4, $5, $6, NOW(), $7, $8)
            RETURNING id
            ''',
            user_id, workspace_id, source_lang, target_lang, original_filename, os.getpid(),
            json.dumps(selected_image_paths) if selected_image_paths else None,
            page_filter or None,
        )
    return row['id']


async def update_job(
    job_id: int,
    status: Optional[str] = None,
    stage_progress: Optional[dict] = None,
    error_type: Optional[str] = None,
    error_msg: Optional[str] = None,
    segment_count: Optional[int] = None,
    needs_translation_count: Optional[int] = None,
    input_volume_path: Optional[str] = None,
    output_volume_path: Optional[str] = None,
) -> None:
    """Best-effort update of a job row; failures are logged, never raised.

    :param job_id: translation_jobs.id.
    :param status: new status; when given, worker_pid, worker_heartbeat, error_type and error_msg are rewritten too.
    :param stage_progress: progress dict stored as JSON; None keeps the current value.
    :param error_type: error class name, stored only together with a status; None clears it.
    :param error_msg: error message, stored only together with a status; None clears it.
    :param segment_count: number of extracted segments; None keeps the current value.
    :param needs_translation_count: number of segments to translate; None keeps the current value.
    :param input_volume_path: storage path of the uploaded original; None keeps the current value.
    :param output_volume_path: storage path of the rebuilt document; None keeps the current value.
    """
    pool = get_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                '''
                UPDATE translation_jobs
                SET status                  = COALESCE($2, status),
                    stage_progress          = COALESCE($3, stage_progress),
                    error_type               = CASE WHEN $2 IS NOT NULL THEN $4 ELSE error_type END,
                    error_msg                = CASE WHEN $2 IS NOT NULL THEN $5 ELSE error_msg END,
                    segment_count            = COALESCE($6, segment_count),
                    needs_translation_count  = COALESCE($7, needs_translation_count),
                    input_volume_path        = COALESCE($8, input_volume_path),
                    output_volume_path       = COALESCE($9, output_volume_path),
                    worker_pid               = CASE WHEN $2 IS NOT NULL THEN $10 ELSE worker_pid END,
                    worker_heartbeat         = CASE WHEN $2 IS NOT NULL THEN NOW() ELSE worker_heartbeat END,
                    updated_at               = NOW()
                WHERE id = $1
                ''',
                job_id, status,
                json.dumps(stage_progress) if stage_progress is not None else None,
                error_type, error_msg, segment_count, needs_translation_count,
                input_volume_path, output_volume_path, os.getpid(),
            )
    except Exception as e:
        logger.error('translate job %s: failed to update status=%s: %s', job_id, status, e)


async def get_dnt_rows() -> List[Dict[str, str]]:
    """Load the do-not-translate rules.

    :return: rows with pattern, type, match_mode and notes; empty without a database.
    """
    pool = get_pool()
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT pattern, type, match_mode, notes FROM dnt_rules')
    return [dict(r) for r in rows]


async def get_glossary_rows() -> List[Dict[str, str]]:
    """Load every glossary term in all supported languages.

    :return: one dict per term keyed by language code; empty without a database.
    """
    pool = get_pool()
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT en, fr, cs, bg, de, es, pt, ar FROM glossary_terms')
    return [dict(r) for r in rows]


async def record_llm_usage(job_id: int, endpoint: str, batch_size: int, usage: Dict[str, int]) -> None:
    """Persist the token usage and cost of one translation LLM call and add it to the job totals.

    :param job_id: translation_jobs.id the call belongs to.
    :param endpoint: serving endpoint name the call went to.
    :param batch_size: number of strings sent in the call.
    :param usage: token counts with keys input_tokens and output_tokens; missing keys count as 0.
    """
    pool = get_pool()
    if not pool:
        return
    input_tokens = usage.get('input_tokens', 0)
    output_tokens = usage.get('output_tokens', 0)
    cost = cost_eur(endpoint, input_tokens, output_tokens)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                '''
                INSERT INTO translation_llm_calls
                    (job_id, endpoint_name, batch_size, input_tokens, output_tokens, cost_eur)
                VALUES ($1, $2, $3, $4, $5, $6)
                ''',
                job_id, endpoint, batch_size, input_tokens, output_tokens, cost,
            )
            await conn.execute(
                '''
                UPDATE translation_jobs SET
                    llm_call_count = llm_call_count + 1,
                    total_input_tokens = total_input_tokens + $2,
                    total_output_tokens = total_output_tokens + $3,
                    total_cost_eur = total_cost_eur + $4
                WHERE id = $1
                ''',
                job_id, input_tokens, output_tokens, cost,
            )
    except Exception as e:
        logger.debug('translate job %s: record_llm_usage failed: %s', job_id, e)


async def insert_segments(job_id: int, audited_segments: List[Dict[str, Any]]) -> None:
    """Insert the audited segments of a job in one transaction; existing (job_id, seg_id) rows are kept.

    :param job_id: translation_jobs.id.
    :param audited_segments: segment dicts as produced by the audit stage, text under 'text'.
    """
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            for s in audited_segments:
                await conn.execute(
                    '''
                    INSERT INTO translation_segments
                        (job_id, seg_id, part, location_type, xml_choice_path, xml_fallback_path,
                         source_text, detected_lang, lang_confidence, pattern_type, pair_id,
                         conflict_flag, conflict_detail, dnt_tokens, inline_split, out_of_page_range)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                    ON CONFLICT (job_id, seg_id) DO NOTHING
                    ''',
                    job_id, s['seg_id'], s.get('part'), s.get('location_type'),
                    json.dumps(s.get('xml_choice_path')), json.dumps(s.get('xml_fallback_path')),
                    s['text'], s.get('detected_lang'), s.get('lang_confidence'),
                    s.get('pattern_type'), s.get('pair_id'), bool(s.get('conflict_flag')),
                    s.get('conflict_detail'),
                    json.dumps(s.get('dnt_tokens') or []), json.dumps(s.get('inline_split')),
                    bool(s.get('out_of_page_range')),
                )


async def insert_questions(job_id: int, questions: List[Dict[str, Any]]) -> None:
    """Insert the audit questions of a job in one transaction; existing (job_id, q_id) rows are kept.

    :param job_id: translation_jobs.id.
    :param questions: question dicts as produced by the audit stage.
    """
    pool = get_pool()
    if not pool or not questions:
        return
    async with pool.acquire() as conn:
        async with conn.transaction():
            for q in questions:
                await conn.execute(
                    '''
                    INSERT INTO translation_questions
                        (job_id, q_id, seg_ids, category, question_text, context, suggested_answer)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (job_id, q_id) DO NOTHING
                    ''',
                    job_id, q['q_id'], json.dumps(q.get('seg_ids') or []),
                    q.get('category'), q.get('question_text'), q.get('context'),
                    q.get('suggested_answer'),
                )
