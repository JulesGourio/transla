"""Preview routes: before/after documents, exact PDFs, page diff images and the render-engine status."""

import asyncio
import base64
import hashlib
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from ...services import storage as _storage
from ...services.lakebase import get_pool
from ...services.soffice import SofficeUnavailable, convert_docx_to_pdf, soffice_status
from ...services.translation.job_files import (
    PREVIEWABLE_STATUSES,
    download_from_volume,
    generate_preview_pdfs,
    preview_pdf_paths,
)
from ...services.translation.pdf_diff import get_all_page_changes, render_page_diff
from ...services.translation.runtime import spawn
from ...services.user import get_user_identity, require_translate

logger = logging.getLogger(__name__)
router = APIRouter()


def _revalidating_response(content: bytes, media_type: str, request: Request,
                           extra_headers: Optional[Dict[str, str]] = None) -> Response:
    """Build a response that browsers revalidate through an ETag instead of caching blindly.

    :param content: response body.
    :param media_type: content type of the body.
    :param request: incoming request, read for If-None-Match.
    :param extra_headers: headers added to the response.
    :return: a 304 response when If-None-Match matches the body's ETag, else the full response.
    """
    etag = '"' + hashlib.sha1(content).hexdigest() + '"'
    headers = {'Cache-Control': 'private, no-cache', 'ETag': etag, **(extra_headers or {})}
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=content, media_type=media_type, headers=headers)


@router.get('/translate/jobs/{job_id}/preview', dependencies=[Depends(require_translate)])
async def get_translation_preview(job_id: int, request: Request, format: str = 'docx') -> Any:
    """Before and after documents of a finished job for the side-by-side view.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :param format: 'docx' for raw documents, 'pdf' for LibreOffice-rendered PDFs.
    :return: base64-encoded before/after documents, or a JSONResponse with 404, 409 (not rebuilt), 422, 500, 501 (no PDF engine), 502 or 503.
    """
    if format not in ('docx', 'pdf'):
        return JSONResponse({'error': f'Unknown format {format!r} (expected docx or pdf)'}, status_code=422)

    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)

    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT input_volume_path, output_volume_path, status FROM translation_jobs '
            'WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
    if not job:
        return JSONResponse({'error': 'Not found'}, status_code=404)
    if (not job['input_volume_path'] or not job['output_volume_path']
            or job['status'] not in PREVIEWABLE_STATUSES):
        return JSONResponse(
            {'error': 'Preview not available yet (job not rebuilt, or output storage failed)'},
            status_code=409,
        )

    if format == 'pdf':
        # Fast path: PDFs pregenerated at the end of the rebuild stage
        # (generate_preview_pdfs) — two plain downloads, no conversion, no
        # LibreOffice needed (a cold app doesn't even extract the engine).
        before_path, after_path = preview_pdf_paths(job['output_volume_path'])
        try:
            before_pdf, after_pdf = await asyncio.gather(
                download_from_volume(before_path),
                download_from_volume(after_path),
            )
            return {
                'before_pdf_base64': base64.b64encode(before_pdf).decode('ascii'),
                'after_pdf_base64': base64.b64encode(after_pdf).decode('ascii'),
                'pregenerated': True,
            }
        except Exception:
            pass  # not pregenerated (older job) — convert on demand below

        try:
            original_bytes = await download_from_volume(job['input_volume_path'])
            output_bytes = await download_from_volume(job['output_volume_path'])
        except Exception as e:
            logger.error('translate job %s: failed to load documents for preview: %s', job_id, e)
            return JSONResponse({'error': f'Failed to load documents: {e}'}, status_code=500)
        try:
            before_pdf, after_pdf = await asyncio.gather(
                asyncio.to_thread(convert_docx_to_pdf, original_bytes),
                asyncio.to_thread(convert_docx_to_pdf, output_bytes),
            )
        except SofficeUnavailable as e:
            logger.info('translate job %s: pdf preview unavailable: %s', job_id, e)
            return JSONResponse({'error': f'Exact PDF rendering unavailable: {e}'}, status_code=501)
        except Exception as e:
            logger.error('translate job %s: pdf conversion failed: %s', job_id, e)
            return JSONResponse({'error': f'PDF conversion failed: {e}'}, status_code=502)
        # Persist for next time (page refresh, other reviewer, app restart).
        spawn(
            generate_preview_pdfs(job_id, original_bytes, output_bytes, job['output_volume_path'])
        )
        return {
            'before_pdf_base64': base64.b64encode(before_pdf).decode('ascii'),
            'after_pdf_base64': base64.b64encode(after_pdf).decode('ascii'),
        }

    try:
        original_bytes = await download_from_volume(job['input_volume_path'])
        output_bytes = await download_from_volume(job['output_volume_path'])
    except Exception as e:
        logger.error('translate job %s: failed to load documents for preview: %s', job_id, e)
        return JSONResponse({'error': f'Failed to load documents: {e}'}, status_code=500)
    return {
        'before_docx_base64': base64.b64encode(original_bytes).decode('ascii'),
        'after_docx_base64': base64.b64encode(output_bytes).decode('ascii'),
    }


@router.get('/translate/jobs/{job_id}/preview.pdf', dependencies=[Depends(require_translate)])
async def get_translation_preview_pdf(job_id: int, request: Request, side: str = 'before') -> Any:
    """One side of the exact preview as raw PDF bytes.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :param side: 'before' for the original, 'after' for the translation.
    :return: a PDF response, or a JSONResponse with 404, 409, 422, 501, 502 or 503.
    """
    if side not in ('before', 'after'):
        return JSONResponse({'error': f'Unknown side {side!r} (expected before or after)'}, status_code=422)

    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)

    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT input_volume_path, output_volume_path, status FROM translation_jobs '
            'WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
    if not job:
        return JSONResponse({'error': 'Not found'}, status_code=404)
    if (not job['input_volume_path'] or not job['output_volume_path']
            or job['status'] not in PREVIEWABLE_STATUSES):
        return JSONResponse({'error': 'Preview not available yet'}, status_code=409)

    pregen_path = preview_pdf_paths(job['output_volume_path'])[0 if side == 'before' else 1]
    try:
        pdf = await download_from_volume(pregen_path)
        return _revalidating_response(pdf, 'application/pdf', request)
    except Exception:
        pass  # not pregenerated yet — convert this side on demand

    docx_path = job['input_volume_path'] if side == 'before' else job['output_volume_path']
    try:
        docx_bytes = await download_from_volume(docx_path)
        pdf = await asyncio.to_thread(convert_docx_to_pdf, docx_bytes)
    except SofficeUnavailable as e:
        return JSONResponse({'error': f'Exact PDF rendering unavailable: {e}'}, status_code=501)
    except Exception as e:
        logger.error('translate job %s: preview.pdf %s failed: %s', job_id, side, e)
        return JSONResponse({'error': f'PDF conversion failed: {e}'}, status_code=502)
    try:
        await asyncio.to_thread(_storage.upload, pregen_path, pdf)
    except Exception as e:
        logger.warning('translate job %s: preview.pdf persistence failed: %s', job_id, e)
    return _revalidating_response(pdf, 'application/pdf', request)


async def _get_preview_pdfs(job_id: int, identity: Dict[str, Any], extra_cols: str = '') -> tuple:
    """Load the pregenerated before/after PDFs of a finished job.

    :param job_id: translation_jobs.id.
    :param identity: caller identity with user_id.
    :param extra_cols: extra translation_jobs columns to select, each prefixed with a comma.
    :return: (job_row, before_pdf, after_pdf, None) on success, else (None, None, None, JSONResponse) carrying the error to return.
    """
    pool = get_pool()
    if not pool:
        return None, None, None, JSONResponse({'error': 'Translation history not available'}, status_code=503)

    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            f'SELECT output_volume_path, status{extra_cols} FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
    if not job:
        return None, None, None, JSONResponse({'error': 'Not found'}, status_code=404)
    if not job['output_volume_path'] or job['status'] not in PREVIEWABLE_STATUSES:
        return None, None, None, JSONResponse({'error': 'Preview not available yet'}, status_code=409)

    before_path, after_path = preview_pdf_paths(job['output_volume_path'])
    try:
        before_pdf, after_pdf = await asyncio.gather(
            download_from_volume(before_path), download_from_volume(after_path),
        )
    except Exception:
        return None, None, None, JSONResponse(
            {'error': 'Preview PDFs not generated yet — open the side-by-side preview first'},
            status_code=409,
        )
    return job, before_pdf, after_pdf, None


@router.get('/translate/jobs/{job_id}/preview/diff.png', dependencies=[Depends(require_translate)])
async def get_translation_preview_diff(job_id: int, request: Request, page: int = 1) -> Any:
    """Pixel-level diff image of one page: grayscale before with the changes in red.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :param page: 1-based page number.
    :return: a PNG response with the page count in X-Total-Pages, or a JSONResponse with 404, 409, 422 or 503.
    """
    if page < 1:
        return JSONResponse({'error': 'page must be >= 1'}, status_code=422)

    identity = await get_user_identity(request)
    _job, before_pdf, after_pdf, error = await _get_preview_pdfs(job_id, identity)
    if error:
        return error

    png, total_pages = await asyncio.to_thread(render_page_diff, before_pdf, after_pdf, page)
    if png is None:
        return JSONResponse(
            {'error': f'Page {page} out of range (document has {total_pages} page(s))'}, status_code=404,
        )
    return _revalidating_response(png, 'image/png', request, {'X-Total-Pages': str(total_pages)})


@router.get('/translate/jobs/{job_id}/preview/changes', dependencies=[Depends(require_translate)])
async def get_translation_preview_changes(job_id: int, request: Request) -> Any:
    """Bounding boxes of every detected change across all pages.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :return: {'changes': [...]}, or a JSONResponse with 404, 409 or 503.
    """
    identity = await get_user_identity(request)
    _job, before_pdf, after_pdf, error = await _get_preview_pdfs(job_id, identity)
    if error:
        return error

    changes = await asyncio.to_thread(get_all_page_changes, before_pdf, after_pdf)
    return {'changes': changes}


@router.get('/translate/render-engine', dependencies=[Depends(require_translate)])
async def get_render_engine_status() -> Any:
    """Report whether the exact-PDF rendering engine is usable on this deployment.

    :return: the engine status; the first call may extract the engine archive and take a minute.
    """
    return await asyncio.to_thread(soffice_status)
