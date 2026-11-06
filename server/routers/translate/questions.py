"""Route recording the reviewer's answers to the audit questions."""

from typing import Any, List

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...services.lakebase import get_pool
from ...services.translation.jobs_db import update_job
from ...services.user import get_user_identity, require_translate

router = APIRouter()


class AnswerItem(BaseModel):
    q_id: str
    answer: str


class AnswerRequest(BaseModel):
    answers: List[AnswerItem]


@router.post('/translate/jobs/{job_id}/answer', dependencies=[Depends(require_translate)])
async def answer_translation_questions(job_id: int, body: AnswerRequest, request: Request) -> Any:
    """Record the reviewer's answers to the audit questions of a job.

    :param job_id: translation_jobs.id.
    :param body: answers keyed by question id.
    :param request: incoming request; its identity scopes the job to its owner.
    :return: {'remaining_questions': unanswered count}, or a JSONResponse with 404, 409 unless the job is awaiting answers, or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)

    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT status FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
        if not job:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        if job['status'] != 'awaiting_answers':
            return JSONResponse(
                {'error': f"Job is not awaiting answers (status={job['status']})"},
                status_code=409,
            )

        async with conn.transaction():
            for a in body.answers:
                await conn.execute(
                    '''
                    UPDATE translation_questions
                    SET answer = $3, answered_at = NOW()
                    WHERE job_id = $1 AND q_id = $2
                    ''',
                    job_id, a.q_id, a.answer,
                )

        remaining = await conn.fetchval(
            'SELECT COUNT(*) FROM translation_questions WHERE job_id = $1 AND answer IS NULL',
            job_id,
        )

    if remaining == 0:
        await update_job(job_id, status='answered')

    return {'remaining_questions': remaining}
