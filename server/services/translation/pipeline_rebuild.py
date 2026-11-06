"""Rebuild stage of a job: fit check, length adaptation, rebuild, validation with one automatic re-translation
pass, flag persistence and output storage."""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List

from .. import storage as _storage
from ..lakebase import get_pool
from ..processors.translation import (
    adapt_lengths,
    build_rebuild_inputs,
    check_fit,
    extract_docx_segments,
    find_unplaced_translations,
    rebuild_docx_bytes,
    validate_docx,
)
from .comments import inject_comments
from .job_files import discard_preview_pdfs, download_from_volume, generate_preview_pdfs
from .jobs_db import get_glossary_rows, heartbeat_loop, update_job
from .pipeline_translate import apply_resolved_segment
from .review_comments import generate_review_comments
from .runtime import ACTIVE_JOBS_SEMAPHORE, get_llm_credentials, spawn
from .segment_plan import plan_segment_translation, segment_category
from .translate_batches import translate_unique_strings

logger = logging.getLogger(__name__)


async def run_rebuild_stage(job_id: int, source_lang: str, include_review_comments: bool = False) -> None:
    """Run fit check, length adaptation, rebuild and validation of a job, then store the output.

    :param job_id: translation_jobs.id, already claimed in status 'fit_checking'.
    :param source_lang: source language code.
    :param include_review_comments: True to anchor Word review comments on flagged segments in the output.
    """
    pool = get_pool()
    # Held only while this stage actively runs; released on completion or failure below.
    await ACTIVE_JOBS_SEMAPHORE.acquire()
    beat = spawn(heartbeat_loop(job_id))
    try:
        async with pool.acquire() as conn:
            job = await conn.fetchrow(
                'SELECT input_volume_path, target_lang FROM translation_jobs WHERE id = $1', job_id,
            )
        if not job or not job['input_volume_path']:
            raise RuntimeError(
                'Original document not available for rebuild (no input_volume_path — '
                'the input upload to storage failed)'
            )

        async with pool.acquire() as conn:
            dismissed_seg_ids = {
                r['seg_id'] for r in await conn.fetch(
                    'SELECT seg_id FROM translation_segments WHERE job_id = $1 AND warning_dismissed', job_id,
                )
            }

        await discard_preview_pdfs(job_id)
        await update_job(job_id, status='fit_checking')
        src_bytes = await download_from_volume(job['input_volume_path'])
        extract_segments = await asyncio.to_thread(extract_docx_segments, src_bytes)

        def _json_or_none(value) -> Any:
            """Decode a stored JSON column.

            :param value: JSON text or None.
            :return: the decoded value, None for an empty column or a JSON null.
            """
            if not value:
                return None
            parsed = json.loads(value)
            return parsed  # json 'null' → None

        # Up to one extra lap: if the rebuilt document still reads as the
        # source language somewhere (a mis-detected/wrongly-kept segment),
        # automatically re-translate exactly those segments and rebuild again
        # before settling for a manual-review warning — most of these are
        # fixable by just asking the LLM again, no human needed.
        already_retried = False
        while True:
            async with pool.acquire() as conn:
                seg_rows = await conn.fetch(
                    '''
                    SELECT id, seg_id, part, location_type, xml_choice_path, xml_fallback_path, pattern_type,
                           inline_split, source_text, translated_text, keep_as_is, dnt_tokens,
                           detected_lang, conflict_flag, conflict_detail, out_of_page_range
                    FROM translation_segments WHERE job_id = $1
                    ''',
                    job_id,
                )

            # Fill part/XML paths from the fresh extraction when the DB row lacks them: the source document is
            # the ground truth for positions.
            ext_by_id = {s['seg_id']: s for s in extract_segments}
            combined = []
            missing_translation_ids: List[str] = []
            # docx_image_ocr segments (see docx_images.py) have no XML position (xml_choice_path holds a media
            # filename), so they never reach the rebuild functions. Their translation is shown only in the
            # Segments panel's Images category: a paragraph placed in the document flow lands far from images
            # that sit in headers, footers or text boxes.
            for r in seg_rows:
                if r['location_type'] == 'docx_image_ocr':
                    continue
                ext = ext_by_id.get(r['seg_id'], {})
                # A segment planned for translation that has none (a lost write) keeps its source text and is
                # flagged.
                no_translation = r['translated_text'] is None and not r['keep_as_is']
                if no_translation:
                    missing_translation_ids.append(r['seg_id'])
                combined.append({
                    'seg_id': r['seg_id'],
                    'part': r['part'] or ext.get('part'),
                    'xml_choice_path': _json_or_none(r['xml_choice_path']) or ext.get('xml_choice_path'),
                    'xml_fallback_path': _json_or_none(r['xml_fallback_path']) or ext.get('xml_fallback_path'),
                    'pattern_type': r['pattern_type'],
                    'inline_split': _json_or_none(r['inline_split']),
                    'original_text': r['source_text'],
                    # Untranslated (keep_as_is) segments rebuild to their own source
                    # text — nothing changes for them, they just get walked/rewritten.
                    'translated_text': r['source_text'] if (r['keep_as_is'] or no_translation) else r['translated_text'],
                    'keep_as_is': bool(r['keep_as_is']) or no_translation,
                })

            flagged, critical = await asyncio.to_thread(check_fit, extract_segments, combined)
            combined, suggestions = await asyncio.to_thread(
                adapt_lengths, extract_segments, combined, job['target_lang'],
            )

            await update_job(job_id, status='rebuilding')
            translations_by_part, metadata = build_rebuild_inputs(combined)
            rebuilt_bytes = await asyncio.to_thread(rebuild_docx_bytes, src_bytes, translations_by_part, metadata)
            unplaced_ids = await asyncio.to_thread(find_unplaced_translations, rebuilt_bytes, combined)

            comments_placed = 0
            if include_review_comments:
                # Categorize off the RAW seg_rows (translated_text nullable) —
                # not `combined` (backfilled with source_text for keep_as_is rows
                # so rebuild always has something to write), which would
                # otherwise misclassify every kept-as-is segment as 'translated'.
                segments_for_review = []
                for r in seg_rows:
                    d = dict(r)
                    d['category'] = segment_category(d)
                    segments_for_review.append(d)
                review_comments = generate_review_comments(segments_for_review, flagged)
                if review_comments:
                    xml_choice_paths = {c['seg_id']: c['xml_choice_path'] for c in combined if c.get('xml_choice_path')}
                    rebuilt_bytes, comments_placed = await asyncio.to_thread(
                        inject_comments, rebuilt_bytes, review_comments, xml_choice_paths,
                    )
                    logger.info('translate job %s: injected %d/%d review comment(s)',
                                job_id, comments_placed, len(review_comments))

            await update_job(job_id, status='validating')
            validation = await asyncio.to_thread(
                validate_docx, rebuilt_bytes, src_bytes, extract_segments, source_lang,
            )

            # Structural checks (zip/xml/namespaces/content-types/fidelity/byte
            # identity) decide pass/fail — a broken file must never reach the user.
            # residual_source_language is a QUALITY signal: a 99%-translated
            # document with two stubborn strings is usable and flagged for review,
            # not a failed job.
            structural = {
                k: v for k, v in validation.items()
                if k not in ('all_passed', 'residual_source_language')
            }
            structural_ok = all(v['passed'] for v in structural.values())
            residual = validation.get('residual_source_language')
            residual_items = residual.get('items', []) if residual and not residual['passed'] else []
            # A reviewer-dismissed segment (e.g. a proper noun the check keeps flagging) never counts, neither
            # for the auto-retry pass below nor for the warning list. DNT, numeric-only and out-of-page-range
            # segments stay in the source language by design and never count either.
            seg_row_by_id = {r['seg_id']: r for r in seg_rows}
            residual_items = [
                it for it in residual_items
                if it['seg_id'] not in dismissed_seg_ids
                and not (it['seg_id'] in seg_row_by_id and (
                    seg_row_by_id[it['seg_id']]['out_of_page_range']
                    or seg_row_by_id[it['seg_id']]['pattern_type'] in ('dnt', 'numeric_only')))
            ]

            if not structural_ok or already_retried or not residual_items:
                break

            endpoint = os.getenv('TRANSLATE_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''))
            host, token = get_llm_credentials()
            if not (endpoint and host and token):
                break  # can't retry without LLM credentials — surface as-is

            # Re-plan through plan_segment_translation, forcing the source language the residual check
            # established, so a bilingual-inline segment re-sends only its source side.
            retry_plans: Dict[str, Dict[str, Any]] = {}
            for it in residual_items:
                r = seg_row_by_id.get(it['seg_id'])
                if not r or not r['source_text'] or it['seg_id'] in retry_plans:
                    continue
                row = dict(r)
                try:
                    row['inline_split'] = json.loads(r['inline_split']) if r['inline_split'] else None
                except (TypeError, json.JSONDecodeError):
                    row['inline_split'] = None
                row.update(detected_lang=source_lang, lang_confidence=None)
                plan = plan_segment_translation(row, source_lang, job['target_lang'], 'monolingual')
                if plan:
                    retry_plans[it['seg_id']] = {'row': row, 'plan': plan}
            unique_strings = list(dict.fromkeys(p['plan']['query'] for p in retry_plans.values()))
            if not unique_strings:
                break
            already_retried = True
            logger.info('translate job %s: %d segment(s) still read as %s — running one automatic re-translation pass',
                        job_id, len(retry_plans), source_lang.upper())
            glossary_rows = await get_glossary_rows()
            retried, _ = await translate_unique_strings(
                host, token, endpoint, unique_strings, source_lang, job['target_lang'], glossary_rows, job_id,
                retry=True,
            )
            if not retried:
                break
            by_source: Dict[str, Dict[str, List[int]]] = {}
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for sid, p in retry_plans.items():
                        new_span = retried.get(p['plan']['query'])
                        if not new_span or new_span == p['plan']['query']:
                            continue
                        await apply_resolved_segment(conn, p['row'], p['plan'], new_span, by_source, source_lang)
                        await conn.execute(
                            'UPDATE translation_segments SET keep_as_is = FALSE WHERE id = $1', p['row']['id'],
                        )
            # Loop again: refetch seg_rows with the re-translated text and redo fit_check, rebuild and validate.

        output_path = f'{_storage.job_root()}/{job_id}/output.docx'
        try:
            await asyncio.to_thread(_storage.upload, output_path, rebuilt_bytes)
        except Exception as e:
            logger.warning('translate job %s: failed to upload output to volume: %s', job_id, e)
            output_path = None

        # Residual-language and fit_check CRITICAL findings are persisted as conflict flags so the Segments
        # panel's Flagged tab agrees with the job status page.
        quality_flag_reasons: Dict[str, str] = {}
        for sid in unplaced_ids:
            quality_flag_reasons[sid] = 'Translation could not be placed in the document'
        for sid in missing_translation_ids:
            quality_flag_reasons[sid] = 'Translation missing — use the translate button'
        for it in residual_items:
            quality_flag_reasons[it['seg_id']] = f"Still reads as {source_lang.upper()} after translation"
        for f in flagged:
            if f['severity'] == 'CRITICAL':
                quality_flag_reasons[f['seg_id']] = (
                    quality_flag_reasons.get(f['seg_id'], '') and quality_flag_reasons[f['seg_id']] + '; '
                ) + 'Translation may not fit its box (text overflow)'
        # These flags describe this rebuild's output: flags left by an earlier rebuild are cleared first.
        async with pool.acquire() as conn:
            cleared = {
                r['seg_id'] for r in await conn.fetch(
                    '''
                    UPDATE translation_segments SET conflict_flag = FALSE, conflict_detail = NULL
                    WHERE job_id = $1 AND (conflict_detail LIKE 'Still reads as %'
                                           OR conflict_detail LIKE 'Translation may not fit its box%'
                                           OR conflict_detail LIKE 'Translation missing%'
                                           OR conflict_detail LIKE 'Translation could not be placed%')
                    RETURNING seg_id
                    ''',
                    job_id,
                )
            }
        seg_rows = [{**dict(r), 'conflict_flag': False} if r['seg_id'] in cleared else r for r in seg_rows]
        if quality_flag_reasons:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for sid, detail in quality_flag_reasons.items():
                        await conn.execute(
                            '''
                            UPDATE translation_segments
                            SET conflict_flag = TRUE, conflict_detail = COALESCE(conflict_detail, $3)
                            WHERE job_id = $1 AND seg_id = $2
                            ''',
                            job_id, sid, detail,
                        )
            seg_rows = [
                {**dict(r), 'conflict_flag': True} if r['seg_id'] in quality_flag_reasons else r
                for r in seg_rows
            ]

        # 'done_with_warnings' distinguishes a structurally-valid document that
        # still needs a human look (residual source-language text even after
        # the automatic re-pass, or segments flagged during translation) from
        # a genuinely clean 'done' — a reviewer shouldn't read plain "Done" as
        # "nothing left to check".
        has_quality_warnings = bool(residual_items) or any(r['conflict_flag'] for r in seg_rows)
        final_status = (
            'failed' if not structural_ok
            else 'done_with_warnings' if has_quality_warnings
            else 'done'
        )
        failed_checks = {k: v['errors'] for k, v in structural.items() if not v['passed']}
        await update_job(
            job_id, status=final_status,
            output_volume_path=output_path,
            error_type=None if structural_ok else 'ValidationFailed',
            error_msg=None if structural_ok else json.dumps(failed_checks, ensure_ascii=False)[:2000],
            stage_progress={
                'stage': final_status,
                'fit_check_flagged': len(flagged),
                'fit_check_critical': critical,
                # seg_ids let the UI jump straight to the offending segments.
                'fit_check_items': [
                    {'seg_id': f['seg_id'], 'severity': f['severity'], 'text': f['translated_text']}
                    for f in flagged[:30]
                ],
                'length_adapt_suggestions': len(suggestions),
                'validation': {k: v['passed'] for k, v in validation.items() if k != 'all_passed'},
                'residual_warnings': residual_items[:20],
                'review_comments_placed': comments_placed,
            },
        )
        logger.info('translate job %s: rebuild stage complete, status=%s', job_id, final_status)

        # Warm the exact-PDF preview while the user is still reading the
        # status panel: both byte buffers are already in memory here, so the
        # side-by-side preview is a plain download by the time it opens.
        if final_status in ('done', 'done_with_warnings') and output_path:
            spawn(generate_preview_pdfs(job_id, src_bytes, rebuilt_bytes, output_path))
        # Glossary candidates are proposed only once a reviewer explicitly
        # validates the document (POST /translate/jobs/{id}/validate) — never
        # automatically here, however the job turned out.
    except Exception as e:
        logger.error('translate job %s: rebuild stage failed: %s', job_id, e, exc_info=True)
        await update_job(job_id, status='failed', error_type=type(e).__name__, error_msg=str(e)[:2000])
    finally:
        beat.cancel()
        ACTIVE_JOBS_SEMAPHORE.release()
