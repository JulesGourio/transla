"""Routes that start the translation and rebuild stages of a job."""

import os
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ...services.lakebase import get_pool
from ...services.translation.pipeline_rebuild import run_rebuild_stage
from ...services.translation.pipeline_translate import run_translation_stage
from ...services.translation.runtime import spawn
from ...services.user import get_user_identity, require_translate

router = APIRouter()


@router.post('/translate/jobs/{job_id}/translate', dependencies=[Depends(require_translate)])
async def start_translation(job_id: int, request: Request) -> Any:
    """Start the batched translation stage of an answered (or retryable failed) job.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :return: {'status': 'translating'}, or a JSONResponse with 404, 409 (wrong status, nothing audited, or claimed concurrently) or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)

    identity = await get_user_identity(request)
    allowed_statuses = ['answered', 'failed']
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT status, source_lang, target_lang, needs_translation_count FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
        if not job:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        # 'failed' is retryable when the audit completed (needs_translation_count
        # set) — transient LLM/endpoint failures shouldn't force a new job.
        if job['status'] not in allowed_statuses:
            return JSONResponse(
                {'error': f"Job is not ready for translation (status={job['status']})"},
                status_code=409,
            )
        if job['status'] == 'failed' and job['needs_translation_count'] is None:
            return JSONResponse(
                {'error': 'Job failed before the audit completed — start a new job'},
                status_code=409,
            )
        # Atomically claim the job: the UPDATE re-checks the status against the locked row, so a double-click or
        # a second browser tab cannot start two concurrent translation runs on the same job.
        claimed = await conn.fetchval(
            '''
            UPDATE translation_jobs
            SET status = 'translating', worker_pid = $3, worker_heartbeat = NOW(), updated_at = NOW()
            WHERE id = $1 AND status = ANY($2)
            RETURNING id
            ''',
            job_id, allowed_statuses, os.getpid(),
        )
    if not claimed:
        return JSONResponse({'error': 'Job status changed — refresh and try again'}, status_code=409)

    spawn(run_translation_stage(job_id, job['source_lang'], job['target_lang']))
    return {'status': 'translating'}


@router.post('/translate/jobs/{job_id}/rebuild', dependencies=[Depends(require_translate)])
async def start_rebuild(job_id: int, request: Request, include_review_comments: bool = False) -> Any:
    """Start fit check, length adaptation, rebuild and validation of a translated job.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :param include_review_comments: True to anchor Word review comments on flagged segments in the output.
    :return: {'status': 'fit_checking'}, or a JSONResponse with 404, 409 (wrong status, segments without translation, or claimed concurrently) or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)

    identity = await get_user_identity(request)
    allowed_statuses = ['translated', 'failed', 'done', 'done_with_warnings']
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT status, source_lang FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
        if not job:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        # 'failed' is retryable once translations exist — a rebuild/validation
        # failure (or transient volume error) shouldn't force re-translating.
        # 'done' is re-runnable too: regenerate the output after segment fixes
        # (the rebuild is idempotent and overwrites output.docx).
        if job['status'] not in allowed_statuses:
            return JSONResponse(
                {'error': f"Job is not ready for rebuild (status={job['status']})"},
                status_code=409,
            )
        if job['status'] in ('failed', 'done', 'done_with_warnings'):
            # Every segment planned for translation (keep_as_is = FALSE) must have translated_text before a
            # rebuild is allowed.
            still_missing = await conn.fetchval(
                '''SELECT EXISTS(SELECT 1 FROM translation_segments
                                 WHERE job_id = $1 AND NOT keep_as_is AND translated_text IS NULL)''',
                job_id,
            )
            if still_missing:
                return JSONResponse(
                    {'error': 'Job has segments still missing a translation — run the translation step first'},
                    status_code=409,
                )
        # Atomically claim the job: the UPDATE re-checks the status, so a double-click or a second tab cannot
        # start two concurrent rebuilds writing output.docx.
        claimed = await conn.fetchval(
            '''
            UPDATE translation_jobs
            SET status = 'fit_checking', worker_pid = $3, worker_heartbeat = NOW(), updated_at = NOW()
            WHERE id = $1 AND status = ANY($2)
            RETURNING id
            ''',
            job_id, allowed_statuses, os.getpid(),
        )
    if not claimed:
        return JSONResponse({'error': 'Job status changed — refresh and try again'}, status_code=409)

    spawn(run_rebuild_stage(job_id, job['source_lang'], include_review_comments))
    return {'status': 'fit_checking'}
