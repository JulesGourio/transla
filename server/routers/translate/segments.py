"""Segment review routes: listing, retranslation, language override, warning dismissal and manual edits."""

import json
import os
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ...services.lakebase import get_pool
from ...services.translation.jobs_db import ACTIVE_PROCESSING_STATUSES, get_glossary_rows
from ...services.translation.runtime import get_llm_credentials
from ...services.translation.segment_plan import INLINE_PATTERNS, plan_segment_translation, segment_category
from ...services.translation.translate_batches import translate_batch
from ...services.user import get_user_identity, require_translate

router = APIRouter()


@router.get('/translate/jobs/{job_id}/segments', dependencies=[Depends(require_translate)])
async def list_translation_segments(
    job_id: int, request: Request,
    category: str = '', limit: int = 200, offset: int = 0,
) -> Any:
    """Per-segment review view of a job: translated, kept as is (and why), flagged.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :param category: segment category to keep, 'flagged' for flagged segments, '' for all.
    :param limit: page size.
    :param offset: rows to skip within the filtered list.
    :return: counts per category, the requested page of segments in document order, or a JSONResponse with 404 or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)

    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT source_lang FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
        if not job:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        rows = await conn.fetch(
            '''
            SELECT seg_id, source_text, translated_text, detected_lang,
                   lang_confidence, pattern_type, keep_as_is, conflict_flag, conflict_detail,
                   location_type, xml_choice_path, out_of_page_range
            FROM translation_segments WHERE job_id = $1 ORDER BY id
            ''',
            job_id,
        )

    counts: Dict[str, int] = {}
    flagged_count = 0
    categorized = []
    for idx, r in enumerate(rows):
        d = dict(r)
        cat = segment_category(d)
        counts[cat] = counts.get(cat, 0) + 1
        if d['conflict_flag']:
            flagged_count += 1
        d['category'] = cat
        # absolute 0-based position in document reading order — stable across
        # category filters/pagination, so the UI can show a row number that
        # matches the Review tab's "Segment N" cross-reference.
        d['index'] = idx
        categorized.append(d)

    if category == 'flagged':
        categorized = [d for d in categorized if d['conflict_flag']]
    elif category:
        categorized = [d for d in categorized if d['category'] == category]
    total = len(categorized)
    page = categorized[offset:offset + limit]

    return {
        'segment_count': len(rows),
        'counts': counts,
        'flagged_count': flagged_count,
        'total': total,
        'segments': [
            {
                'seg_id': d['seg_id'],
                'index': d['index'],
                'source_text': d['source_text'],
                'translated_text': d['translated_text'],
                'detected_lang': d['detected_lang'],
                'lang_confidence': d['lang_confidence'],
                'category': d['category'],
                'flagged': d['conflict_flag'],
                'conflict_detail': d['conflict_detail'],
                'thumbnail_base64': (
                    (json.loads(d['xml_choice_path']) or {}).get('thumbnail_base64')
                    if d['category'] == 'image_translation' and d.get('xml_choice_path') else None
                ),
            }
            for d in page
        ],
    }


class SegmentTranslationIn(BaseModel):
    translated_text: str


# seg_id values are XML paths (e.g. "word/document.xml#table.bt5.r5.c0#p0") containing literal '/', so these
# routes use the greedy `:path` converter. The suffixed routes (/retranslate, /language, /dismiss-warning) must be
# declared before the bare .../segments/{seg_id:path} route, or that route swallows the suffix into seg_id.
@router.post(
    '/translate/jobs/{job_id}/segments/{seg_id:path}/retranslate', dependencies=[Depends(require_translate)],
)
async def retranslate_segment(job_id: int, seg_id: str, request: Request) -> Any:
    """Send one segment through the LLM again, whatever the audit decided.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :param seg_id: segment id, an XML path that may contain '/'.
    :return: {'seg_id', 'translated_text'}, or a JSONResponse with 404, 409, 502 or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT source_lang, target_lang, status FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
        if not job:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        if job['status'] in ACTIVE_PROCESSING_STATUSES:
            return JSONResponse({'error': 'The job is still running — retry once it has finished'}, status_code=409)
        seg = await conn.fetchrow(
            'SELECT source_text, pattern_type, inline_split FROM translation_segments '
            'WHERE job_id = $1 AND seg_id = $2',
            job_id, seg_id,
        )
        if not seg:
            return JSONResponse({'error': 'Segment not found'}, status_code=404)
    if not seg['source_text']:
        return JSONResponse({'error': 'Segment has no source text to translate'}, status_code=409)

    # A bilingual-inline segment holds both languages: only its source side goes to the LLM, and the stored
    # text is composed exactly as the translation stage composes it.
    plan = None
    if seg['pattern_type'] in INLINE_PATTERNS:
        try:
            inline = json.loads(seg['inline_split']) if seg['inline_split'] else None
        except (TypeError, json.JSONDecodeError):
            inline = None
        plan = plan_segment_translation(
            {'pattern_type': seg['pattern_type'], 'source_text': seg['source_text'], 'inline_split': inline,
             'detected_lang': job['source_lang'], 'lang_confidence': None, 'out_of_page_range': False},
            job['source_lang'], job['target_lang'], 'monolingual',
        )
    query = plan['query'] if plan else seg['source_text']

    endpoint = os.getenv('TRANSLATE_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''))
    host, token = get_llm_credentials()
    if not (endpoint and host and token):
        return JSONResponse({'error': 'Translation endpoint not configured'}, status_code=503)

    glossary_rows = await get_glossary_rows()
    try:
        result = await translate_batch(
            host, token, endpoint, [query], job['source_lang'], job['target_lang'], glossary_rows, job_id,
        )
    except Exception as e:
        return JSONResponse({'error': f'Translation failed: {e}'}, status_code=502)
    new_text = result.get(query)
    if not new_text:
        return JSONResponse(
            {'error': 'The LLM did not return a translation — try again or edit the text manually'},
            status_code=502,
        )
    if plan:
        new_text = plan['compose'](new_text)

    async with pool.acquire() as conn:
        await conn.execute(
            '''
            UPDATE translation_segments
            SET translated_text = $3, keep_as_is = FALSE, conflict_flag = FALSE, conflict_detail = NULL,
                detected_lang = $4, lang_confidence = NULL, inline_split = COALESCE($5, inline_split)
            WHERE job_id = $1 AND seg_id = $2
            ''',
            job_id, seg_id, new_text, job['source_lang'], plan['inline_json'] if plan else None,
        )
    return {'seg_id': seg_id, 'translated_text': new_text}


class SegmentLanguageIn(BaseModel):
    detected_lang: str


@router.patch(
    '/translate/jobs/{job_id}/segments/{seg_id:path}/language', dependencies=[Depends(require_translate)],
)
async def update_segment_language(job_id: int, seg_id: str, body: SegmentLanguageIn, request: Request) -> Any:
    """Override the detected language of a segment without translating it.

    :param job_id: translation_jobs.id.
    :param seg_id: segment id, an XML path that may contain '/'.
    :param body: the language code to store.
    :param request: incoming request; its identity scopes the job to its owner.
    :return: {'seg_id', 'detected_lang'}, or a JSONResponse with 404 or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT id FROM translation_jobs WHERE id = $1 AND user_id = $2', job_id, identity['user_id'],
        )
        if not job:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        result = await conn.execute(
            '''
            UPDATE translation_segments
            SET detected_lang = $3, lang_confidence = NULL
            WHERE job_id = $1 AND seg_id = $2
            ''',
            job_id, seg_id, body.detected_lang,
        )
        if result == 'UPDATE 0':
            return JSONResponse({'error': 'Segment not found'}, status_code=404)
    return {'seg_id': seg_id, 'detected_lang': body.detected_lang}


@router.post(
    '/translate/jobs/{job_id}/segments/{seg_id:path}/dismiss-warning', dependencies=[Depends(require_translate)],
)
async def dismiss_segment_warning(job_id: int, seg_id: str, request: Request) -> Any:
    """Mark a segment's residual-language warning as accepted; it takes effect at the next rebuild.

    :param request: incoming request; its identity scopes the job to its owner.
    :param job_id: translation_jobs.id.
    :param seg_id: segment id, an XML path that may contain '/'.
    :return: {'seg_id', 'warning_dismissed': True}, or a JSONResponse with 404 or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT id FROM translation_jobs WHERE id = $1 AND user_id = $2', job_id, identity['user_id'],
        )
        if not job:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        result = await conn.execute(
            'UPDATE translation_segments SET warning_dismissed = TRUE WHERE job_id = $1 AND seg_id = $2',
            job_id, seg_id,
        )
        if result == 'UPDATE 0':
            return JSONResponse({'error': 'Segment not found'}, status_code=404)
    return {'seg_id': seg_id, 'warning_dismissed': True}


# Declared last among the /segments/{seg_id}... routes: its unsuffixed :path would otherwise swallow the
# suffixed routes' paths (see the comment above retranslate_segment).
@router.patch(
    '/translate/jobs/{job_id}/segments/{seg_id:path}', dependencies=[Depends(require_translate)],
)
async def update_segment_translation(job_id: int, seg_id: str, body: SegmentTranslationIn, request: Request) -> Any:
    """Store a reviewer's manual translation of a segment; the document is rebuilt separately.

    :param job_id: translation_jobs.id.
    :param seg_id: segment id, an XML path that may contain '/'.
    :param body: the new translation, which must not be blank.
    :param request: incoming request; its identity scopes the job to its owner.
    :return: {'seg_id', 'translated_text'}, or a JSONResponse with 404, 409 while the job runs, 422 for a blank text or 503.
    """
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    if not body.translated_text.strip():
        # An empty paragraph in the document, with the segment shown as
        # human-confirmed: almost certainly a slip, never a translation.
        return JSONResponse({'error': 'The translation cannot be empty'}, status_code=422)
    identity = await get_user_identity(request)
    async with pool.acquire() as conn:
        job = await conn.fetchrow(
            'SELECT source_lang, status FROM translation_jobs WHERE id = $1 AND user_id = $2',
            job_id, identity['user_id'],
        )
        if not job:
            return JSONResponse({'error': 'Not found'}, status_code=404)
        if job['status'] in ACTIVE_PROCESSING_STATUSES:
            # The running stage rewrites translated_text / reads the rows once:
            # an edit made now is overwritten or misses this build.
            return JSONResponse({'error': 'The job is still running — edit once it has finished'}, status_code=409)
        result = await conn.execute(
            '''
            UPDATE translation_segments
            SET translated_text = $3, keep_as_is = FALSE, conflict_flag = FALSE, conflict_detail = NULL,
                detected_lang = $4, lang_confidence = NULL
            WHERE job_id = $1 AND seg_id = $2
            ''',
            job_id, seg_id, body.translated_text, job['source_lang'],
        )
        if result == 'UPDATE 0':
            return JSONResponse({'error': 'Segment not found'}, status_code=404)
    return {'seg_id': seg_id, 'translated_text': body.translated_text}
