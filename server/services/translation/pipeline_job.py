"""First pipeline stages of a job: extraction, optional image OCR, audit, page filter and persistence of the
segments and questions, ending in the awaiting_answers rest state."""

import asyncio
import logging
import os
from typing import List, Optional

from ..processors.translation import audit_segments, extract_docx_segments
from .docx_images import find_image_anchors, ocr_docx_images, order_ocr_segments
from .job_files import upload_input_to_volume
from .jobs_db import (
    get_dnt_rows,
    heartbeat_loop,
    insert_questions,
    insert_segments,
    record_llm_usage,
    update_job,
)
from .pages import assign_pages, parse_page_spec
from .runtime import ACTIVE_JOBS_SEMAPHORE, get_llm_credentials, spawn

logger = logging.getLogger(__name__)


async def run_job(job_id: int, docx_bytes: bytes, filename: str,
                 source_lang: str, target_lang: str,
                 selected_images: Optional[List[str]] = None,
                 page_filter: Optional[str] = None) -> None:
    """Run the first pipeline stages of a job: extract, optional image OCR, audit, page filter, persist.

    :param job_id: translation_jobs.id.
    :param docx_bytes: content of the uploaded .docx.
    :param filename: client file name, used for the stored original.
    :param source_lang: source language code.
    :param target_lang: target language code.
    :param selected_images: media filenames whose embedded text is read by OCR; None or empty disables OCR.
    :param page_filter: page specification restricting what is translated; None or empty translates everything.
    """
    upload_task = spawn(upload_input_to_volume(job_id, docx_bytes, filename))

    # Held only while this stage actively runs (extracting/auditing): released once the job rests in
    # awaiting_answers or fails, so a slow reviewer never ties up a processing slot.
    async with ACTIVE_JOBS_SEMAPHORE:
        beat = spawn(heartbeat_loop(job_id))
        try:
            await update_job(job_id, status='extracting')
            segments = await asyncio.to_thread(extract_docx_segments, docx_bytes)

            # Opt-in, best-effort: OCR text baked into user-selected embedded
            # images (see docx_images.py) — never fails the job, a credentials/
            # endpoint problem just means those images are skipped.
            if selected_images:
                ocr_endpoint = os.getenv('TRANSLATE_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''))
                ocr_host, ocr_token = get_llm_credentials()
                if ocr_endpoint and ocr_host and ocr_token:
                    try:
                        ocr_results, ocr_usage_log = await ocr_docx_images(
                            docx_bytes, selected_images, ocr_host, ocr_token, ocr_endpoint,
                        )
                        for u in ocr_usage_log:
                            await record_llm_usage(job_id, ocr_endpoint, 1, u['usage'])
                        if ocr_results:
                            logger.info('translate job %s: OCR found text in %d/%d selected image(s)',
                                        job_id, len(ocr_results), len(selected_images))
                        image_segments = [{
                            'seg_id': f"imgocr-{r['filename']}",
                            'text': r['text'],
                            'part': None,
                            'location_type': 'docx_image_ocr',
                            'xml_choice_path': {
                                'media_filename': r['filename'],
                                'thumbnail_base64': r.get('thumbnail_base64'),
                            },
                            'xml_fallback_path': None,
                            'body_p_idx': -1,
                            'para_idx_in_container': -1,
                            'fmt_signature': '',
                            'runs': [],
                            'table_coords': None,
                            'txbx_path': None,
                        } for r in ocr_results]
                        # Spliced in at the image's own document position so its segment sits next to the
                        # surrounding text.
                        anchors = await asyncio.to_thread(find_image_anchors, docx_bytes, [r['filename'] for r in ocr_results])
                        segments = order_ocr_segments(segments, image_segments, anchors)
                    except Exception as e:
                        logger.warning('translate job %s: docx image OCR failed, continuing without it: %s',
                                        job_id, e)
                else:
                    logger.info('translate job %s: skipping docx image OCR (no LLM credentials/endpoint)', job_id)

            await update_job(
                job_id, status='auditing',
                segment_count=len(segments),
                stage_progress={'stage': 'auditing', 'done': 0, 'total': len(segments)},
            )

            dnt_rows = await get_dnt_rows()
            audit_report, questions = await asyncio.to_thread(
                audit_segments, segments, dnt_rows, source_lang, target_lang,
            )

            # Opt-in page filter (pages.py): page numbers follow the original docx's LibreOffice rendering.
            # Segments the mapping cannot place stay translatable rather than being silently excluded.
            page_spec = parse_page_spec(page_filter or '')
            if page_spec:
                page_map = await asyncio.to_thread(assign_pages, segments, docx_bytes)
                for s in audit_report['segments']:
                    page = page_map.get(s['seg_id'])
                    s['out_of_page_range'] = page is not None and page not in page_spec

            # Every rebuild and preview starts from the stored original: fail here, before any paid
            # translation, when its upload did not succeed.
            if not await upload_task:
                raise RuntimeError(
                    'The uploaded file could not be saved to storage, so it cannot be rebuilt or previewed — '
                    'upload it again, and contact support if this keeps happening'
                )
            await insert_segments(job_id, audit_report['segments'])
            await insert_questions(job_id, questions)

            needs_translation = sum(
                1 for s in audit_report['segments']
                if s['pattern_type'] not in ('dnt', 'numeric_only') and not s.get('out_of_page_range')
            )
            await update_job(
                job_id, status='awaiting_answers',
                needs_translation_count=needs_translation,
                stage_progress={
                    'stage': 'awaiting_answers',
                    'mode': audit_report.get('mode', 'bilingual'),
                    'source_lang': source_lang,
                    'target_lang': target_lang,
                    'segment_count': audit_report['segment_count'],
                    'pair_count': audit_report['pair_count'],
                    'conflict_count': audit_report['conflict_count'],
                    'question_count': len(questions),
                    'primary_language': audit_report['primary_language'],
                    'secondary_language': audit_report['secondary_language'],
                },
            )
            logger.info('translate job %s: audit complete (%d segments, %d questions)',
                        job_id, audit_report['segment_count'], len(questions))
        except Exception as e:
            logger.error('translate job %s failed: %s', job_id, e, exc_info=True)
            await update_job(job_id, status='failed', error_type=type(e).__name__, error_msg=str(e)[:2000])
        finally:
            beat.cancel()
