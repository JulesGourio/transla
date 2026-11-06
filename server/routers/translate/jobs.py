"""Job lifecycle routes: upload, status, image listing, restart, notes, sign-off and history."""

import asyncio
import io
import json
import logging
import os
import zipfile
from datetime import datetime
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...services.lakebase import get_pool
from ...services.translation.docx_images import list_docx_images
from ...services.translation.glossary_proposals import propose_glossary_candidates
from ...services.translation.job_files import discard_preview_pdfs, download_from_volume
from ...services.translation.jobs_db import ACTIVE_PROCESSING_STATUSES, create_job
from ...services.translation.pages import parse_page_spec
from ...services.translation.pipeline_job import run_job
from ...services.translation.runtime import spawn
from ...services.user import get_user_identity, require_translate

logger = logging.getLogger(__name__)
router = APIRouter()


MAX_FILE_BYTES = int(os.getenv('MAX_TRANSLATE_FILE_MB', '60')) * 1024 * 1024
# A .docx is a zip: 60 MB compressed can inflate to many GB (every part is read
# whole into memory, several times per job).
MAX_UNZIPPED_BYTES = int(os.getenv('MAX_TRANSLATE_UNZIPPED_MB', '800')) * 1024 * 1024
STALE_HEARTBEAT_S = 120  # matches the restart-reconciliation window in app.py


def _validate_file(file: UploadFile, data: bytes) -> None:
    """Check an uploaded file is a non-empty, reasonably sized, readable .docx.

    :param file: uploaded file; its name must end in .docx.
    :param data: content of the file.
    """
    if not data:
        raise ValueError(f'File "{file.filename or "unknown"}" is empty')
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f'File "{file.filename or "unknown"}" exceeds {MAX_FILE_BYTES // (1024*1024)} MB limit')
    if not (file.filename or '').lower().endswith('.docx'):
        raise ValueError('Only .docx files are supported (bilingual aerospace documents)')
    # Rejected at upload with a readable message (password-protected files and renamed .doc files land here).
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except zipfile.BadZipFile:
        raise ValueError(f'"{file.filename}" is not a valid .docx (it may be corrupted, password-protected, or an old .doc renamed)')
    if not any(i.filename == 'word/document.xml' for i in infos):
        raise ValueError(f'"{file.filename}" is not a Word document (no word/document.xml inside)')
    if sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
        raise ValueError(f'"{file.filename}" is too large once unzipped (limit {MAX_UNZIPPED_BYTES // (1024*1024)} MB)')


@router.post('/translate/jobs', dependencies=[Depends(require_translate)])
async def create_translation_job(
    request: Request,
    file: UploadFile = File(...),
    source_lang: str = Form(...),
    target_lang: str = Form(...),
    selected_images: str = Form(''),
    pages: str = Form(''),
) -> Any:
    """Upload a .docx, create its job and start the analysis in the background.

    :param request: incoming request, used to identify the user.
    :param file: .docx to translate.
    :param source_lang: source language code.
    :param target_lang: target language code.
    :param selected_images: JSON array of media filenames to read by OCR, '' for none.
    :param pages: page specification such as '1-3,7', '' for all pages.
    :return: {'id': job_id}, or a JSONResponse with 400 for an invalid file or field and 503 without a database.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available (LAKEBASE_PROJECT_ID not configured)'}, status_code=503)

    try:
        data = await file.read()
        _validate_file(file, data)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=400)

    try:
        selected_image_paths = json.loads(selected_images) if selected_images.strip() else []
        if not isinstance(selected_image_paths, list):
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        return JSONResponse({'error': 'selected_images must be a JSON array of filenames'}, status_code=400)

    try:
        parse_page_spec(pages)
    except ValueError:
        return JSONResponse({'error': 'pages must look like "3-10", "1,4,9", or "1-3,7,12-15"'}, status_code=400)

    identity = await get_user_identity(request)
    filename = file.filename or 'document.docx'
    job_id = await create_job(
        user_id=identity['user_id'], workspace_id=identity.get('workspace_id'),
        source_lang=source_lang.strip().lower(), target_lang=target_lang.strip().lower(),
        original_filename=filename, selected_image_paths=selected_image_paths,
        page_filter=pages.strip(),
    )
    spawn(run_job(
        job_id, data, filename,
        source_lang.strip().lower(), target_lang.strip().lower(),
        selected_image_paths, pages.strip(),
    ))
    return {'id': job_id}


@router.post('/translate/analyze-images', dependencies=[Depends(require_translate)])
async def analyze_docx_images(file: UploadFile = File(...)) -> Any:
    """List the embedded raster images of an uploaded .docx with a thumbnail each; nothing is stored.

    :param file: .docx to inspect.
    :return: {'images': [...]}, or a JSONResponse with 400 for an invalid file and 422 when images cannot be read.
    """
    try:
        data = await file.read()
        _validate_file(file, data)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=400)
    try:
        images = await asyncio.to_thread(list_docx_images, data)
    except Exception as e:
        logger.warning('analyze-images failed for %s: %s', file.filename, e)
        return JSONResponse({'error': f'Could not read images: {e}'}, status_code=422)
    return {'images': images}


@router.get('/translate/jobs/{job_id}/images', dependencies=[Depends(require_translate)])
async def get_job_images(job_id: int, request: Request) -> Any:
    """List the embedded images of a job's stored original and the selection made when it was created.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :return: {'images': [...], 'previously_selected': [media filenames]}, or a JSONResponse with 404, 409 when the original is not stored, 422 or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT input_volume_path, selected_image_paths FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
    if not job:
        return JSONResponse({'error': 'Not found'}, status_code=404)
    if not job['input_volume_path']:
        return JSONResponse({'error': 'Original upload not available for this job'}, status_code=409)
    try:
        docx_bytes = await download_from_volume(job['input_volume_path'])
        images = await asyncio.to_thread(list_docx_images, docx_bytes)
    except Exception as e:
        logger.warning('translate job %s: get_job_images failed: %s', job_id, e)
        return JSONResponse({'error': f'Could not read images: {e}'}, status_code=422)
    previously_selected = json.loads(job['selected_image_paths']) if job['selected_image_paths'] else []
    return {'images': images, 'previously_selected': previously_selected}


@router.get('/translate/jobs/{job_id}', dependencies=[Depends(require_translate)])
async def get_translation_job(job_id: int, request: Request) -> Any:
    """Status of a job with its progress, totals and, while they matter, its questions.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :return: job fields including 'stale' (active status with no recent heartbeat), or a JSONResponse with 404 or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)

    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT * FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
        if not row:
            return JSONResponse({'error': 'Not found'}, status_code=404)

        questions = []
        if row['status'] in ('awaiting_answers', 'answered'):
            q_rows = await conn.fetch(
                '''
                SELECT q_id, seg_ids, category, question_text, context, suggested_answer, answer
                FROM translation_questions WHERE job_id = $1 ORDER BY id
                ''',
                job_id,
            )
            questions = [
                {**dict(q), 'seg_ids': json.loads(q['seg_ids'] or '[]')}
                for q in q_rows
            ]

    heartbeat = row['worker_heartbeat']
    stale = (
        row['status'] in ACTIVE_PROCESSING_STATUSES
        and heartbeat is not None
        and (datetime.now(heartbeat.tzinfo) - heartbeat).total_seconds() > STALE_HEARTBEAT_S
    )

    return {
        'id': row['id'],
        'status': row['status'],
        'stage_progress': json.loads(row['stage_progress']) if row['stage_progress'] else None,
        'source_lang': row['source_lang'],
        'target_lang': row['target_lang'],
        'original_filename': row['original_filename'],
        'segment_count': row['segment_count'],
        'needs_translation_count': row['needs_translation_count'],
        'error_type': row['error_type'],
        'error_msg': row['error_msg'],
        'stale': stale,
        'questions': questions,
        'llm_call_count': row['llm_call_count'],
        'total_input_tokens': row['total_input_tokens'],
        'total_output_tokens': row['total_output_tokens'],
        'total_cost_eur': row['total_cost_eur'],
        'notes': row['notes'],
        'glossary_validated_at': row['glossary_validated_at'].isoformat() if row['glossary_validated_at'] else None,
    }


class RestartIn(BaseModel):
    # None keeps the job's stored selection; an explicit list (including []) is the choice the reviewer
    # confirmed in the image picker.
    selected_images: Optional[List[str]] = None


@router.post('/translate/jobs/{job_id}/restart', dependencies=[Depends(require_translate)])
async def restart_translation_job(job_id: int, request: Request, body: RestartIn = RestartIn()) -> Any:
    """Rerun the whole pipeline on the stored original, wiping segments, questions and manual edits.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :param body: optional new image selection; None keeps the job's previous selection, a list replaces it.
    :return: {'id': job_id}, or a JSONResponse with 404, 409 (job running or original not stored), 502 or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT * FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
    if not job:
        return JSONResponse({'error': 'Not found'}, status_code=404)
    if job['status'] in ACTIVE_PROCESSING_STATUSES:
        return JSONResponse({'error': 'Job is still running — wait for it to finish or fail first'}, status_code=409)
    if not job['input_volume_path']:
        return JSONResponse(
            {'error': 'The original upload is unavailable for this job — upload the file again as a new job'},
            status_code=409,
        )

    try:
        docx_bytes = await download_from_volume(job['input_volume_path'])
    except Exception as e:
        return JSONResponse({'error': f'Could not retrieve the original upload: {e}'}, status_code=502)

    selected_image_paths = (
        body.selected_images if body.selected_images is not None
        else (json.loads(job['selected_image_paths']) if job['selected_image_paths'] else [])
    )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute('DELETE FROM translation_segments WHERE job_id = $1', job_id)
            await conn.execute('DELETE FROM translation_questions WHERE job_id = $1', job_id)
            await conn.execute(
                '''
                UPDATE translation_jobs
                SET status = 'uploaded', stage_progress = NULL, error_type = NULL, error_msg = NULL,
                    segment_count = NULL, needs_translation_count = NULL, output_volume_path = NULL,
                    llm_call_count = 0, total_input_tokens = 0, total_output_tokens = 0, total_cost_eur = 0,
                    glossary_validated_at = NULL, selected_image_paths = $3,
                    worker_pid = $2, worker_heartbeat = NOW(), updated_at = NOW()
                WHERE id = $1
                ''',
                job_id, os.getpid(), json.dumps(selected_image_paths) if selected_image_paths else None,
            )

    await discard_preview_pdfs(job_id)
    spawn(run_job(
        job_id, docx_bytes, job['original_filename'], job['source_lang'], job['target_lang'],
        selected_image_paths, job['page_filter'],
    ))
    return {'id': job_id}


class JobNotesIn(BaseModel):
    notes: str


@router.patch('/translate/jobs/{job_id}/notes', dependencies=[Depends(require_translate)])
async def update_job_notes(job_id: int, body: JobNotesIn, request: Request) -> Any:
    """Save the reviewer's free-text notes on a job.

    :param job_id: translation_jobs.id.
    :param body: the notes text.
    :param request: incoming request; its identity scopes the job to its owner.
    :return: {'notes': text}, or a JSONResponse with 404 or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        result = await conn.execute(
            'UPDATE translation_jobs SET notes = $3, updated_at = NOW() WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'], body.notes,
        )
        if result == 'UPDATE 0':
            return JSONResponse({'error': 'Not found'}, status_code=404)
    return {'notes': body.notes}


@router.post('/translate/jobs/{job_id}/validate', dependencies=[Depends(require_translate)])
async def validate_translation_job(job_id: int, request: Request) -> Any:
    """Record the reviewer's sign-off of a finished job and start the glossary proposals.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :return: {'glossary_validated_at': ISO timestamp}, or a JSONResponse with 404, 409 unless the job is done, or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT status, source_lang, target_lang FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
        if not job:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        if job['status'] not in ('done', 'done_with_warnings'):
            return JSONResponse(
                {'error': f"Job must be done before it can be validated (status={job['status']})"},
                status_code=409,
            )
        row = await conn.fetchrow(
            'UPDATE translation_jobs SET glossary_validated_at = NOW(), updated_at = NOW() '
            'WHERE id = $1 RETURNING glossary_validated_at',
            job_id,
        )

    if job['target_lang']:
        spawn(propose_glossary_candidates(job_id, job['source_lang'], job['target_lang']))
    return {'glossary_validated_at': row['glossary_validated_at'].isoformat()}


@router.get('/translate/jobs', dependencies=[Depends(require_translate)])
async def list_translation_jobs(request: Request, limit: int = 20, offset: int = 0) -> Any:
    """List the caller's jobs, newest first.

    :param request: incoming request; its identity scopes the job to its owner.
    :param limit: page size, clamped to 1..200.
    :param offset: rows to skip, clamped to >= 0.
    :return: {'jobs': [...], 'total': count, 'available': bool}; available is False without a database.
    """
    pool = get_pool()
    if not pool:
        return {'jobs': [], 'total': 0, 'available': False}

    limit, offset = max(1, min(limit, 200)), max(0, offset)  # a negative LIMIT is a Postgres error (500)
    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''
            SELECT id, created_at, status, original_filename, source_lang, target_lang,
                   segment_count, needs_translation_count
            FROM translation_jobs
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            ''',
            identity['user_id'], limit, offset,
        )
        total = await conn.fetchval(
            'SELECT COUNT(*) FROM translation_jobs WHERE user_id = $1', identity['user_id'],
        )

    jobs = []
    for r in rows:
        d = dict(r)
        d['created_at'] = d['created_at'].isoformat() if d['created_at'] else None
        jobs.append(d)
    return {'jobs': jobs, 'total': total, 'available': True}
