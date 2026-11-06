"""Job files in storage: the stored original, and the pregenerated preview PDFs next to the output document."""

import asyncio
import logging
import re
import os

from .. import storage as _storage
from ..soffice import SofficeUnavailable, convert_docx_to_pdf
from .jobs_db import update_job

logger = logging.getLogger(__name__)


def sanitize_filename(filename: str, fallback: str = 'document') -> str:
    """Reduce an uploaded file name to a storage-safe name of at most 100 characters.

    :param filename: name as sent by the client; any directory part is dropped.
    :param fallback: name returned when nothing usable remains.
    :return: name made of letters, digits, '.', '_' and '-', keeping the extension.
    """
    base = os.path.basename((filename or '').strip())
    if not base:
        return fallback
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._')
    # Truncate the stem, not the extension, to stay within the file-name limit.
    if len(safe) > 100:
        stem, dot, ext = safe.rpartition('.')
        safe = (stem[:100 - len(ext) - 1] + dot + ext) if dot else safe[:100]
    return safe or fallback


async def download_from_volume(path: str) -> bytes:
    """Read a file from job storage without blocking the event loop.

    :param path: storage path of the file.
    :return: the file content.
    """
    return await asyncio.to_thread(_storage.download, path)


def preview_pdf_paths(output_volume_path: str) -> tuple[str, str]:
    """Storage paths of the pregenerated preview PDFs, next to the output document.

    :param output_volume_path: storage path of output.docx.
    :return: (original_pdf_path, translated_pdf_path).
    """
    job_dir = output_volume_path.rsplit('/', 1)[0]
    return f'{job_dir}/preview_original.pdf', f'{job_dir}/preview_translated.pdf'


# The previews and the shared view show output.docx: only a finished job's file
# has passed the structural checks (a 'failed' one may be broken) and is not the
# previous rebuild's leftover while a new one runs.
PREVIEWABLE_STATUSES = ('done', 'done_with_warnings')


async def discard_preview_pdfs(job_id: int) -> None:
    """Delete the pregenerated preview PDFs of a job; deletion failures are logged.

    :param job_id: translation_jobs.id.
    """
    for path in preview_pdf_paths(f'{_storage.job_root()}/{job_id}/output.docx'):
        try:
            await asyncio.to_thread(_storage.delete, path)
        except Exception as e:
            logger.warning('translate job %s: could not delete stale preview %s: %s', job_id, path, e)


async def generate_preview_pdfs(
    job_id: int, src_bytes: bytes, rebuilt_bytes: bytes, output_volume_path: str,
) -> None:
    """Convert both documents to PDF and store them next to the output; best effort, failures are logged.

    :param job_id: translation_jobs.id.
    :param src_bytes: content of the original .docx.
    :param rebuilt_bytes: content of the rebuilt .docx.
    :param output_volume_path: storage path of output.docx, which fixes where the PDFs are written.
    """
    try:
        before_pdf, after_pdf = await asyncio.gather(
            asyncio.to_thread(convert_docx_to_pdf, src_bytes),
            asyncio.to_thread(convert_docx_to_pdf, rebuilt_bytes),
        )
    except SofficeUnavailable as e:
        logger.info('translate job %s: preview PDF warmup skipped: %s', job_id, e)
        return
    except Exception as e:
        logger.warning('translate job %s: preview PDF warmup failed: %s', job_id, e)
        return
    before_path, after_path = preview_pdf_paths(output_volume_path)
    try:
        await asyncio.to_thread(_storage.upload, before_path, before_pdf)
        await asyncio.to_thread(_storage.upload, after_path, after_pdf)
        logger.info('translate job %s: preview PDFs persisted to volume', job_id)
    except Exception as e:
        logger.warning('translate job %s: preview PDF volume upload failed: %s', job_id, e)


async def upload_input_to_volume(job_id: int, docx_bytes: bytes, filename: str) -> bool:
    """Store the uploaded original under the job's folder and record its path on the job.

    :param job_id: translation_jobs.id.
    :param docx_bytes: content of the uploaded .docx.
    :param filename: client file name, sanitized before use.
    :return: True when the file is stored, False when the upload failed.
    """
    dest = f'{_storage.job_root()}/{job_id}/input_{sanitize_filename(filename)}'
    try:
        await asyncio.to_thread(_storage.upload, dest, docx_bytes)
        await update_job(job_id, input_volume_path=dest)
        return True
    except Exception as e:
        logger.warning('translate job %s: background volume upload failed: %s', job_id, e)
        return False
