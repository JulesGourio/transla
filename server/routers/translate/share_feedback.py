"""Feedback and read-only sharing routes."""

import base64
import logging
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...services.lakebase import get_pool
from ...services.translation.job_files import PREVIEWABLE_STATUSES, download_from_volume
from ...services.user import get_user_identity, get_workspace_url, require_translate

logger = logging.getLogger(__name__)
router = APIRouter()


class FeedbackRequest(BaseModel):
    vote: str          # "up" or "down"
    comment: Optional[str] = None
    job_id: Optional[int] = None


@router.post('/translate/feedback', dependencies=[Depends(require_translate)])
async def submit_feedback(body: FeedbackRequest, request: Request) -> Any:
    """Store a thumbs up or down with an optional comment.

    :param body: vote ('up' or 'down'), optional comment and job id.
    :param request: incoming request; its identity scopes the job to its owner.
    :return: {'success': True, 'id', 'vote'}, or a JSONResponse with 422, 500 or 503.
    """
    if body.vote not in ('up', 'down'):
        return JSONResponse({'error': 'vote must be "up" or "down"'}, status_code=422)

    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Feedback unavailable (database not configured)'}, status_code=503)

    identity = await get_user_identity(request)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                '''
                INSERT INTO translation_feedbacks (job_id, user_id, workspace_id, workspace_url, vote, comment)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id, created_at, vote
                ''',
                body.job_id,
                identity['user_id'],
                identity.get('workspace_id'),
                get_workspace_url(),
                body.vote,
                body.comment or None,
            )
        return {'success': True, 'id': row['id'], 'vote': row['vote']}
    except Exception as e:
        logger.error(f'Failed to save feedback: {e}')
        return JSONResponse({'error': str(e)}, status_code=500)


@router.post('/translate/jobs/{job_id}/share', dependencies=[Depends(require_translate)])
async def share_translation_job(job_id: int, request: Request) -> Any:
    """Create, or return, the read-only share token of a job.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :return: {'share_token': token}; an already shared job keeps its token. A JSONResponse with 404 or 503 otherwise.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Database not configured'}, status_code=503)

    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT share_token FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
        if not job:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        token = job['share_token']
        if not token:
            token = secrets.token_urlsafe(20)
            await conn.execute(
                'UPDATE translation_jobs SET share_token = $2 WHERE id = $1',
                job_id, token,
            )
    return {'share_token': token}


@router.get('/translate/shared/{token}', dependencies=[Depends(require_translate)])
async def get_shared_translation_job(token: str) -> Any:
    """Read-only view of a shared job for any authenticated user holding the token.

    :param token: share token.
    :return: job summary, segments, latest feedback and the translated .docx in base64 when the job is done, or a JSONResponse with 404 or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Database not configured'}, status_code=503)

    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            '''
            SELECT id, created_at, status, original_filename, source_lang, target_lang,
                   segment_count, needs_translation_count, output_volume_path
            FROM translation_jobs WHERE share_token = $1
            ''',
            token,
        )
        if not job:
            return JSONResponse({'error': 'Shared translation not found'}, status_code=404)

        segments = await conn.fetch(
            '''
            SELECT seg_id, source_text, translated_text, conflict_flag
            FROM translation_segments WHERE job_id = $1 ORDER BY id
            ''',
            job['id'],
        )
        feedback = await conn.fetchrow(
            '''
            SELECT vote, comment FROM translation_feedbacks
            WHERE job_id = $1 ORDER BY created_at DESC LIMIT 1
            ''',
            job['id'],
        )

    output_docx_base64 = None
    if job['output_volume_path'] and job['status'] in PREVIEWABLE_STATUSES:
        try:
            output_bytes = await download_from_volume(job['output_volume_path'])
            output_docx_base64 = base64.b64encode(output_bytes).decode('ascii')
        except Exception as e:
            logger.warning(f'shared job {job["id"]}: failed to load translated document: {e}')

    return {
        'id': job['id'],
        'created_at': job['created_at'].isoformat() if job['created_at'] else None,
        'status': job['status'],
        'original_filename': job['original_filename'],
        'source_lang': job['source_lang'],
        'target_lang': job['target_lang'],
        'segment_count': job['segment_count'],
        'needs_translation_count': job['needs_translation_count'],
        'segments': [
            {
                'seg_id': s['seg_id'],
                'source_text': s['source_text'],
                'translated_text': s['translated_text'],
                'flagged': s['conflict_flag'],
            }
            for s in segments
        ],
        'feedback': {'vote': feedback['vote'], 'comment': feedback['comment']} if feedback else None,
        'output_docx_base64': output_docx_base64,
    }
