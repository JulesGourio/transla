"""Translate router — bilingual .docx translation pipeline (Translate tab).

Full pipeline: upload -> extract -> audit -> awaiting_answers -> answered
-> translating -> translated -> fit_checking -> rebuilding -> validating
-> done | failed, plus a before/after preview once rebuilt.

Background jobs run as asyncio.create_task fire-and-forget coroutines (no
task queue in this app — see server/services/lakebase.py for the equivalent
pattern used by /compare/analyze's background volume upload). Each stage
transition refreshes worker_pid/worker_heartbeat so a startup reconciliation
pass (server/app.py lifespan) can detect and fail jobs orphaned by a restart.
"""

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import secrets
import time
import zipfile
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from databricks.sdk import WorkspaceClient
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from ..services.lakebase import get_pool
from ..services.processors.translation import (
    adapt_lengths,
    audit_segments,
    build_rebuild_inputs,
    check_fit,
    extract_docx_segments,
    find_unplaced_translations,
    rebuild_docx_bytes,
    validate_docx,
)
from ..services import storage as _storage
from ..services.soffice import SofficeUnavailable, convert_docx_to_pdf, soffice_status
from ..services.llm import call_llm_json, cost_eur
from ..services.translation.audit import determine_mode as _determine_mode
from ..services.translation.comments import inject_comments
from ..services.translation.docx_images import find_image_anchors, list_docx_images, ocr_docx_images, order_ocr_segments
from ..services.translation.glossary_io import dnt_rule_problem
from ..services.translation.langdetect import normalize_hyphens
from ..services.translation.glossary_extract import extract_job_candidates, normalize_term
from ..services.translation import glossary_lakebase as _glossary_lakebase
from ..services.translation.glossary_lakebase import generate_term_id
from ..services.translation.glossary_verify import llm_verify
from ..services.translation.pages import assign_pages, parse_page_spec
from ..services.translation.pdf_diff import get_all_page_changes, render_page_diff
from ..services.translation.review_comments import generate_review_comments
from ..services.user import get_user_identity, get_workspace_url, require_translate

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_FILE_BYTES = int(os.getenv('MAX_TRANSLATE_FILE_MB', '60')) * 1024 * 1024
# A .docx is a zip: 60 MB compressed can inflate to many GB (every part is read
# whole into memory, several times per job).
MAX_UNZIPPED_BYTES = int(os.getenv('MAX_TRANSLATE_UNZIPPED_MB', '800')) * 1024 * 1024
_STALE_HEARTBEAT_S = 120  # matches the restart-reconciliation window in app.py

# Statuses where a worker is (or should be) actively processing — these are the
# only ones whose heartbeat going stale means the job is genuinely orphaned. The
# resting states awaiting_answers / answered / translated are user-gated: no
# worker runs while the reviewer decides, so their heartbeat naturally goes
# stale and they must SURVIVE a restart (resumable by re-polling), not be failed
# or flagged stalled. 'uploaded' is included: _run_job starts right after
# create, so an 'uploaded' job with a dead worker never got picked up.
ACTIVE_PROCESSING_STATUSES = (
    'uploaded', 'extracting', 'auditing', 'translating',
    'fit_checking', 'rebuilding', 'validating',
)

_TRANSLATE_BATCH_SIZE = 40
# call_llm_json caps the answer at 8192 tokens: 40 long paragraphs in one batch
# overran it, the JSON came back cut off, and every batch of such a document
# failed twice before falling back to one call per string.
_TRANSLATE_BATCH_MAX_CHARS = int(os.getenv('TRANSLATE_BATCH_MAX_CHARS', '6000'))
_translate_semaphore = asyncio.Semaphore(int(os.getenv('TRANSLATE_MAX_CONCURRENT', '3')))
# A single job's batches must not monopolize the whole _translate_semaphore
# pool while another job's batches queue behind it — this caps any one job's
# share, acquired in addition to (nested inside) the global semaphore.
_TRANSLATE_MAX_CONCURRENT_PER_JOB = int(os.getenv('TRANSLATE_MAX_CONCURRENT_PER_JOB', '2'))
# Caps how many jobs can be actively processed (extracting/auditing/translating/
# fit_checking/rebuilding/validating) at once across all users in this process —
# protects the default asyncio thread-pool executor and the Lakebase pool from
# an unbounded number of concurrent CPU-bound stages. Upload itself is never
# blocked by this — only the background processing of a job waits its turn.
_active_jobs_semaphore = asyncio.Semaphore(int(os.getenv('TRANSLATE_MAX_CONCURRENT_JOBS', '5')))
_LANG_NAMES = {'en': 'English', 'fr': 'French', 'es': 'Spanish', 'de': 'German', 'cs': 'Czech', 'bg': 'Bulgarian',
               'pt': 'Portuguese', 'ar': 'Arabic'}

_TRANSLATE_SYSTEM_PROMPT = """You are a professional aerospace maintenance-documentation translator.

Translate the given {source_name} strings into {target_name}. These are segments extracted from \
bilingual aerospace assembly/maintenance instructions (LATECOERE) — safety-critical, technical content.

Rules:
- Preserve technical meaning exactly.
- Keep part numbers, standards references, and codes unchanged (do not translate them).
- Reuse the provided glossary for any matching term.
- Input is a JSON object {{"items": [{{"id": "<id>", "text": "<source string>"}}, ...]}}.
- Return ONLY a JSON object of the exact shape {{"translations": {{"<id>": "<translated string>", ...}}}} \
— one entry per input id, no extra prose, no markdown fence."""

# The same prompt again mostly echoes the same untranslated output — the
# residual re-pass has to say why these strings are back.
_RETRY_PROMPT_SUFFIX = """IMPORTANT: a previous pass returned these strings still in {source_name}. \
Every {source_name} word must now be translated into {target_name}; only part numbers, codes, \
standards references and proper nouns may stay unchanged."""


# asyncio keeps only a weak reference to a running task: a fire-and-forget one
# can be garbage-collected mid-run (the job then sits in its active status until
# the stale-heartbeat reconciliation fails it).
_background_tasks: set = set()


def _spawn(coro) -> 'asyncio.Task':
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


_HEARTBEAT_S = 30


async def _heartbeat_loop(job_id: int) -> None:
    """Keep worker_heartbeat fresh while a stage runs. It was only touched at
    stage transitions, so a long LLM batch (up to 5 min) read as 'stalled' in the
    UI, and a second app instance's startup reconciliation could fail a job that
    was alive on the first."""
    while True:
        await asyncio.sleep(_HEARTBEAT_S)
        pool = get_pool()
        if not pool:
            continue
        try:
            async with pool.acquire() as conn:
                await conn.execute('UPDATE translation_jobs SET worker_heartbeat = NOW() WHERE id = $1', job_id)
        except Exception as e:
            logger.debug('translate job %s: heartbeat failed: %s', job_id, e)


def _sanitize_filename(filename: str, fallback: str = 'document') -> str:
    base = os.path.basename((filename or '').strip())
    if not base:
        return fallback
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._')
    # keeps the extension; a very long name exceeded the file-name limit and the upload failed
    if len(safe) > 100:
        stem, dot, ext = safe.rpartition('.')
        safe = (stem[:100 - len(ext) - 1] + dot + ext) if dot else safe[:100]
    return safe or fallback


def _validate_file(file: UploadFile, data: bytes) -> None:
    if not data:
        raise ValueError(f'File "{file.filename or "unknown"}" is empty')
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f'File "{file.filename or "unknown"}" exceeds {MAX_FILE_BYTES // (1024*1024)} MB limit')
    if not (file.filename or '').lower().endswith('.docx'):
        raise ValueError('Only .docx files are supported (bilingual aerospace documents)')
    # Checked now so the user is told on the spot, instead of a job that fails
    # later with a raw "File is not a zip file" (password-protected files and
    # renamed .doc files land here).
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
    except zipfile.BadZipFile:
        raise ValueError(f'"{file.filename}" is not a valid .docx (it may be corrupted, password-protected, or an old .doc renamed)')
    if not any(i.filename == 'word/document.xml' for i in infos):
        raise ValueError(f'"{file.filename}" is not a Word document (no word/document.xml inside)')
    if sum(i.file_size for i in infos) > MAX_UNZIPPED_BYTES:
        raise ValueError(f'"{file.filename}" is too large once unzipped (limit {MAX_UNZIPPED_BYTES // (1024*1024)} MB)')


# ---------------------------------------------------------------------------
# Job persistence helpers
# ---------------------------------------------------------------------------

async def _create_job(user_id: str, workspace_id: Optional[str], source_lang: str,
                       target_lang: str, original_filename: str,
                       selected_image_paths: Optional[List[str]] = None,
                       page_filter: Optional[str] = None) -> int:
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


async def _update_job(
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
    """Best-effort job update; always refreshes worker_pid/worker_heartbeat
    when status is provided (i.e. this process is actively driving the job).
    error_type/error_msg only change together with a status: the background
    input upload (no status) used to land after a fast failure and wipe its
    message, leaving a failed job that said nothing."""
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


async def _get_dnt_rows() -> List[Dict[str, str]]:
    pool = get_pool()
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT pattern, type, match_mode, notes FROM dnt_rules')
    return [dict(r) for r in rows]


async def _get_glossary_rows() -> List[Dict[str, str]]:
    pool = get_pool()
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch('SELECT en, fr, cs, bg, de, es, pt, ar FROM glossary_terms')
    return [dict(r) for r in rows]


_GLOSSARY_MAX_LINES = 300


def _build_glossary_context(
    glossary_rows: List[Dict[str, str]], source_lang: str, target_lang: str,
    batch_strings: Optional[List[str]] = None,
) -> str:
    """Glossary lines for the system prompt.

    When batch_strings is given, only terms actually PRESENT in the batch are
    included — sending the whole table on every call bloats each prompt and
    drowns the relevant terms as the glossary grows.
    """
    blob = '\n'.join(batch_strings).lower() if batch_strings else None
    lines = []
    for row in glossary_rows:
        src = (row.get(source_lang) or '').strip()
        tgt = (row.get(target_lang) or '').strip()
        if not src or not tgt:
            continue
        if blob is not None and src.lower() not in blob:
            continue
        lines.append(f'{src} -> {tgt}')
        if len(lines) >= _GLOSSARY_MAX_LINES:
            break
    return '\n'.join(lines)


def _get_llm_credentials() -> tuple[str, str]:
    """Resolve (host, token) for a detached background task.

    Unlike a request-scoped call, there's no forwarded per-request token to
    fall back on here (the HTTP request that kicked off the job has already
    returned) — this relies entirely on the service-principal auth Databricks
    Apps provide via env vars / WorkspaceClient, the same source the
    background volume-upload task in compare.py uses.
    """
    host = os.environ.get('DATABRICKS_HOST', '').rstrip('/')
    if host and not host.startswith('http'):
        host = f'https://{host}'
    token = os.environ.get('DATABRICKS_TOKEN', '')
    if not host or not token:
        try:
            from databricks.sdk.core import Config
            cfg = Config()
            if not host:
                host = (cfg.host or '').rstrip('/')
            if not token:
                auth_value = cfg.authenticate().get('Authorization', '')
                if auth_value.startswith('Bearer '):
                    token = auth_value[len('Bearer '):]
        except Exception as e:
            logger.debug('SDK auth unavailable: %s', e)
    return host, token


async def _record_llm_usage(job_id: int, endpoint: str, batch_size: int, usage: Dict[str, int]) -> None:
    """Persist one translate LLM call's token usage (best-effort, never raises)."""
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
        logger.debug('translate job %s: _record_llm_usage failed: %s', job_id, e)


_MEMORY_MAX_LINES = 100


def _build_memory_context(resolved_memory: Dict[str, str], batch_strings: List[str]) -> str:
    """Already-resolved translations from earlier batches of THIS job whose
    source form appears in the current batch — reused to keep terminology
    consistent across batches without a shared prompt/session."""
    blob = '\n'.join(batch_strings).lower()
    lines = []
    for src, tgt in resolved_memory.items():
        if src.lower() not in blob:
            continue
        lines.append(f'{src} -> {tgt}')
        if len(lines) >= _MEMORY_MAX_LINES:
            break
    return '\n'.join(lines)


async def _translate_batch(
    host: str, token: str, endpoint: str, strings: List[str],
    source_lang: str, target_lang: str, glossary_rows: List[Dict[str, str]], job_id: int,
    job_semaphore: Optional[asyncio.Semaphore] = None,
    resolved_memory: Optional[Dict[str, str]] = None,
    retry: bool = False,
) -> Dict[str, str]:
    """Translate one batch. Items are keyed by numeric id — requiring the LLM
    to echo the exact source string as a JSON key (previous design) broke on
    strings with quotes/newlines and caused spurious retries."""
    source_name = _LANG_NAMES.get(source_lang, source_lang.upper())
    target_name = _LANG_NAMES.get(target_lang, target_lang.upper())
    system_prompt = _TRANSLATE_SYSTEM_PROMPT.format(source_name=source_name, target_name=target_name)
    if retry:
        system_prompt += '\n\n' + _RETRY_PROMPT_SUFFIX.format(source_name=source_name, target_name=target_name)
    glossary_context = _build_glossary_context(glossary_rows, source_lang, target_lang, batch_strings=strings)
    if glossary_context:
        system_prompt += f'\n\nGlossary ({source_lang}->{target_lang}):\n{glossary_context}'
    memory_context = _build_memory_context(resolved_memory, strings) if resolved_memory else ''
    if memory_context:
        system_prompt += f'\n\nAlready translated in this document (reuse for consistency):\n{memory_context}'
    items = [{'id': str(i), 'text': s} for i, s in enumerate(strings)]
    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': json.dumps({'items': items}, ensure_ascii=False)},
    ]
    # job_semaphore acquired first (outer) so a job never holds a scarce
    # global _translate_semaphore slot while waiting for its own per-job cap.
    # Optional (defaults to no per-job cap) so this function stays directly
    # callable/testable without a caller-managed semaphore.
    async with (job_semaphore or contextlib.AsyncExitStack()):
        async with _translate_semaphore:
            result, usage = await call_llm_json(host, token, endpoint, messages)
    await _record_llm_usage(job_id, endpoint, len(strings), usage)
    raw = result.get('translations', {}) if isinstance(result, dict) else {}
    out: Dict[str, str] = {}
    for i, s in enumerate(strings):
        value = raw.get(str(i))
        if isinstance(value, str) and value.strip():
            out[s] = value
    return out


def _make_batches(strings: List[str]) -> List[List[str]]:
    """Consecutive groups of at most _TRANSLATE_BATCH_SIZE strings and
    _TRANSLATE_BATCH_MAX_CHARS characters (a single longer string travels alone)."""
    batches: List[List[str]] = []
    current: List[str] = []
    chars = 0
    for s in strings:
        if current and (len(current) >= _TRANSLATE_BATCH_SIZE or chars + len(s) > _TRANSLATE_BATCH_MAX_CHARS):
            batches.append(current)
            current, chars = [], 0
        current.append(s)
        chars += len(s)
    if current:
        batches.append(current)
    return batches


async def _translate_unique_strings(
    host: str, token: str, endpoint: str, unique_strings: List[str],
    source_lang: str, target_lang: str, glossary_rows: List[Dict[str, str]], job_id: int,
    on_resolved: Optional[Callable[[Dict[str, str]], Awaitable[None]]] = None,
    progress_offset: int = 0, progress_total: Optional[int] = None,
    retry: bool = False,
) -> tuple[Dict[str, str], List[str]]:
    """Batch-translate unique strings, retrying failed batches individually.

    Returns (translations, failed_strings). A batch that raises or comes back
    missing entries retries whole, then falls back to per-string calls (2
    attempts each) — a bad batch never fails the whole job; unresolved
    strings are reported back for the caller to flag for manual review.

    on_resolved(newly_resolved), if given, is awaited right after each batch
    (or per-string fallback) yields new query->translation pairs — the caller
    uses this to persist results to the DB incrementally instead of waiting
    for every batch in the job to finish (see _run_translation_stage), so a
    crash partway through a large job doesn't lose already-completed work.
    """
    translations: Dict[str, str] = {}
    failed: List[str] = []
    # Populated as batches complete; later-starting batches (queued behind
    # _translate_semaphore) see terms already resolved by earlier ones,
    # improving cross-batch terminology consistency at no extra LLM cost.
    resolved_memory: Dict[str, str] = {}
    # This job's own share of the global _translate_semaphore pool (see
    # _translate_batch) — keeps one large job from starving another job's
    # batches queued behind the same global cap.
    job_semaphore = asyncio.Semaphore(min(_TRANSLATE_MAX_CONCURRENT_PER_JOB, int(os.getenv('TRANSLATE_MAX_CONCURRENT', '3'))))
    batches = _make_batches(unique_strings)

    async def _run_batch(batch: List[str]) -> None:
        applied: set[str] = set()
        todo = batch
        for attempt in range(2):
            try:
                result = await _translate_batch(
                    host, token, endpoint, todo, source_lang, target_lang, glossary_rows, job_id,
                    job_semaphore, resolved_memory=resolved_memory, retry=retry,
                )
                missing = [s for s in todo if s not in result]
                for s in todo:
                    if s in result:
                        translations[s] = result[s]
                resolved_memory.update(result)
                new_items = {s: result[s] for s in todo if s in result and s not in applied}
                if new_items:
                    if on_resolved:
                        await on_resolved(new_items)
                    # Only once persisted: a failed write (rolled back) must be
                    # retried, not counted as done and left without a translation.
                    applied.update(new_items)
                if not missing:
                    return
                todo = missing  # the retry pays only for what is still unresolved
                logger.warning('translate job %s: batch attempt %d missing %d/%d strings',
                                job_id, attempt + 1, len(missing), len(batch))
            except Exception as e:
                logger.warning('translate job %s: batch of %d failed (attempt %d): %s',
                                job_id, len(todo), attempt + 1, e)

        # Batch-level retries exhausted — fall back to per-string calls so one
        # bad string doesn't sink the whole batch.
        for s in batch:
            if s in applied:
                continue
            for attempt in range(2):
                try:
                    result = await _translate_batch(
                        host, token, endpoint, [s], source_lang, target_lang, glossary_rows, job_id,
                        job_semaphore, resolved_memory=resolved_memory, retry=retry,
                    )
                    if s in result:
                        translations[s] = result[s]
                        resolved_memory.update(result)
                        if on_resolved:
                            await on_resolved({s: result[s]})
                        applied.add(s)
                        break
                except Exception as e:
                    logger.warning('translate job %s: single-string retry failed (attempt %d): %s',
                                    job_id, attempt + 1, e)
            if s not in applied:
                translations.pop(s, None)
                failed.append(s)

    total = progress_total if progress_total is not None else len(unique_strings)
    done_strings = 0

    async def _run_and_count(batch: List[str]) -> int:
        await _run_batch(batch)
        return len(batch)

    tasks = [asyncio.create_task(_run_and_count(b)) for b in batches]
    for finished in asyncio.as_completed(tasks):
        done_strings += await finished
        translated_so_far = progress_offset + min(done_strings, len(unique_strings))
        if retry:
            continue  # runs inside the rebuild stage: don't flip the job status back to 'translating'
        await _update_job(
            job_id, status='translating',
            stage_progress={'stage': 'translating', 'done': min(translated_so_far, total), 'total': total},
        )

    return translations, failed


async def _insert_segments(job_id: int, audited_segments: List[Dict[str, Any]]) -> None:
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


async def _insert_questions(job_id: int, questions: List[Dict[str, Any]]) -> None:
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


# ---------------------------------------------------------------------------
# Background pipeline
# ---------------------------------------------------------------------------

async def _upload_input_to_volume(job_id: int, docx_bytes: bytes, filename: str) -> bool:
    dest = f'{_storage.job_root()}/{job_id}/input_{_sanitize_filename(filename)}'
    try:
        await asyncio.to_thread(_storage.upload, dest, docx_bytes)
        await _update_job(job_id, input_volume_path=dest)
        return True
    except Exception as e:
        logger.warning('translate job %s: background volume upload failed: %s', job_id, e)
        return False


async def _run_job(job_id: int, docx_bytes: bytes, filename: str,
                   source_lang: str, target_lang: str,
                   selected_images: Optional[List[str]] = None,
                   page_filter: Optional[str] = None) -> None:
    upload_task = _spawn(_upload_input_to_volume(job_id, docx_bytes, filename))

    # Held only while this stage actively runs (extracting/auditing) — released
    # the moment the job reaches the human-gated awaiting_answers rest state or
    # fails, so a slow reviewer never ties up a processing slot.
    async with _active_jobs_semaphore:
        beat = _spawn(_heartbeat_loop(job_id))
        try:
            await _update_job(job_id, status='extracting')
            segments = await asyncio.to_thread(extract_docx_segments, docx_bytes)

            # Opt-in, best-effort: OCR text baked into user-selected embedded
            # images (see docx_images.py) — never fails the job, a credentials/
            # endpoint problem just means those images are skipped.
            if selected_images:
                ocr_endpoint = os.getenv('TRANSLATE_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''))
                ocr_host, ocr_token = _get_llm_credentials()
                if ocr_endpoint and ocr_host and ocr_token:
                    try:
                        ocr_results, ocr_usage_log = await ocr_docx_images(
                            docx_bytes, selected_images, ocr_host, ocr_token, ocr_endpoint,
                        )
                        for u in ocr_usage_log:
                            await _record_llm_usage(job_id, ocr_endpoint, 1, u['usage'])
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
                        # Ordered near the image's own location (see
                        # docx_images.py) rather than appended after every
                        # real segment — otherwise every image translation
                        # lands at the very end of the Segments panel list
                        # regardless of where the image actually sits.
                        anchors = await asyncio.to_thread(find_image_anchors, docx_bytes, [r['filename'] for r in ocr_results])
                        segments = order_ocr_segments(segments, image_segments, anchors)
                    except Exception as e:
                        logger.warning('translate job %s: docx image OCR failed, continuing without it: %s',
                                        job_id, e)
                else:
                    logger.info('translate job %s: skipping docx image OCR (no LLM credentials/endpoint)', job_id)

            await _update_job(
                job_id, status='auditing',
                segment_count=len(segments),
                stage_progress={'stage': 'auditing', 'done': 0, 'total': len(segments)},
            )

            dnt_rows = await _get_dnt_rows()
            audit_report, questions = await asyncio.to_thread(
                audit_segments, segments, dnt_rows, source_lang, target_lang,
            )

            # Opt-in page filter (pages.py): resolved against the ORIGINAL
            # docx's own LibreOffice-rendered layout, since that's the closest
            # thing to "page N" a reviewer would recognize. Segments the
            # mapping couldn't confidently place are left translatable rather
            # than silently excluded — same reasoning as never dropping OCR'd
            # image segments elsewhere in this pipeline.
            page_spec = parse_page_spec(page_filter or '')
            if page_spec:
                page_map = await asyncio.to_thread(assign_pages, segments, docx_bytes)
                for s in audit_report['segments']:
                    page = page_map.get(s['seg_id'])
                    s['out_of_page_range'] = page is not None and page not in page_spec

            # The original is what every rebuild and preview starts from: a job
            # whose upload to storage failed used to go through the whole
            # (paid) translation and only fail at rebuild, with a message about
            # a missing path. Failing here says it while it still costs nothing.
            if not await upload_task:
                raise RuntimeError(
                    'The uploaded file could not be saved to storage, so it cannot be rebuilt or previewed — '
                    'upload it again, and contact support if this keeps happening'
                )
            await _insert_segments(job_id, audit_report['segments'])
            await _insert_questions(job_id, questions)

            needs_translation = sum(
                1 for s in audit_report['segments']
                if s['pattern_type'] not in ('dnt', 'numeric_only') and not s.get('out_of_page_range')
            )
            await _update_job(
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
            await _update_job(job_id, status='failed', error_type=type(e).__name__, error_msg=str(e)[:2000])
        finally:
            beat.cancel()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post('/translate/jobs', dependencies=[Depends(require_translate)])
async def create_translation_job(
    request: Request,
    file: UploadFile = File(...),
    source_lang: str = Form(...),
    target_lang: str = Form(...),
    selected_images: str = Form(''),
    pages: str = Form(''),
):
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
    job_id = await _create_job(
        user_id=identity['user_id'], workspace_id=identity.get('workspace_id'),
        source_lang=source_lang.strip().lower(), target_lang=target_lang.strip().lower(),
        original_filename=filename, selected_image_paths=selected_image_paths,
        page_filter=pages.strip(),
    )
    _spawn(_run_job(
        job_id, data, filename,
        source_lang.strip().lower(), target_lang.strip().lower(),
        selected_image_paths, pages.strip(),
    ))
    return {'id': job_id}


@router.post('/translate/analyze-images', dependencies=[Depends(require_translate)])
async def analyze_docx_images(file: UploadFile = File(...)):
    """List embedded raster images in an uploaded .docx with a small
    thumbnail each, for the opt-in "translate text in images" selection UI.
    Stateless — no job is created here, the file isn't persisted."""
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
async def get_job_images(job_id: int, request: Request):
    """Same image list as analyze-images, but for an existing job's already-
    uploaded document — used to let a reviewer re-see (and keep or change)
    the previous image selection before a restart, instead of it being
    silently dropped or blindly reused."""
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
        docx_bytes = await _download_from_volume(job['input_volume_path'])
        images = await asyncio.to_thread(list_docx_images, docx_bytes)
    except Exception as e:
        logger.warning('translate job %s: get_job_images failed: %s', job_id, e)
        return JSONResponse({'error': f'Could not read images: {e}'}, status_code=422)
    previously_selected = json.loads(job['selected_image_paths']) if job['selected_image_paths'] else []
    return {'images': images, 'previously_selected': previously_selected}


@router.get('/translate/jobs/{job_id}', dependencies=[Depends(require_translate)])
async def get_translation_job(job_id: int, request: Request):
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
        and (datetime.now(heartbeat.tzinfo) - heartbeat).total_seconds() > _STALE_HEARTBEAT_S
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
    # None = keep whatever this job's previous selected_image_paths already
    # was (never silently dropped); an explicit list (including []) means
    # the reviewer went through the image picker again and this is their
    # confirmed choice, possibly changed from before.
    selected_images: Optional[List[str]] = None


@router.post('/translate/jobs/{job_id}/restart', dependencies=[Depends(require_translate)])
async def restart_translation_job(job_id: int, request: Request, body: RestartIn = RestartIn()):
    """Redo the whole pipeline from scratch on this job — extract, audit,
    translate, rebuild — reusing the originally uploaded .docx (no re-upload
    needed). Wipes every segment/question and any manual review edit made so
    far; this is a full do-over, not a resume. Refuses while a worker is
    actively driving the job (ACTIVE_PROCESSING_STATUSES) to avoid racing it."""
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
        docx_bytes = await _download_from_volume(job['input_volume_path'])
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

    await _discard_preview_pdfs(job_id)
    _spawn(_run_job(
        job_id, docx_bytes, job['original_filename'], job['source_lang'], job['target_lang'],
        selected_image_paths, job['page_filter'],
    ))
    return {'id': job_id}


class JobNotesIn(BaseModel):
    notes: str


@router.patch('/translate/jobs/{job_id}/notes', dependencies=[Depends(require_translate)])
async def update_job_notes(job_id: int, body: JobNotesIn, request: Request):
    """Free-text reviewer notes on the job — separate from _update_job (which
    drives status/worker-heartbeat transitions and isn't meant for
    human-typed input)."""
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
async def validate_translation_job(job_id: int, request: Request):
    """Explicit human sign-off on a finished document's translation — the
    gate _propose_glossary_candidates waits on. Nothing proposes terms to
    the glossary candidate queue until a reviewer clicks this; re-clicking
    (e.g. after editing a segment and rebuilding) re-runs the extraction
    over the segments' current state."""
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
        _spawn(_propose_glossary_candidates(job_id, job['source_lang'], job['target_lang']))
    return {'glossary_validated_at': row['glossary_validated_at'].isoformat()}


def _segment_category(row: Dict[str, Any]) -> str:
    """Reviewer-facing outcome bucket for one segment, derived from the state
    the translation stage already wrote (translated_text / keep_as_is /
    conflict_flag) — mode-independent:

      image_translation     -> OCR'd from an embedded image (docx_images.py);
                               always its own bucket regardless of translated/
                               kept/pending state, since it needs the image
                               thumbnail shown alongside it, not just text
      translated           -> got a translation (may still carry a flag)
      kept_dnt / kept_numeric -> never translatable by design
      kept_page_filtered    -> outside the job's requested page range (pages.py)
      kept_other_language  -> intentionally left in the target/other language
                               (a confident other-language passage, or the kept
                               side of a bilingual-inline segment)
      needs_review          -> translation failed after all retries; source text
                               kept and flagged
      pending               -> not yet through the translation stage
    """
    if row.get('location_type') == 'docx_image_ocr':
        return 'image_translation'
    if row.get('out_of_page_range'):
        return 'kept_page_filtered'
    if row['pattern_type'] == 'dnt':
        return 'kept_dnt'
    if row['pattern_type'] == 'numeric_only':
        return 'kept_numeric'
    if row['translated_text'] is not None:
        return 'translated'
    if row['keep_as_is']:
        # A failed translation is flagged (conflict_flag) when kept; an
        # intentional keep is not.
        return 'needs_review' if row.get('conflict_flag') else 'kept_other_language'
    return 'pending'


@router.get('/translate/jobs/{job_id}/segments', dependencies=[Depends(require_translate)])
async def list_translation_segments(
    job_id: int, request: Request,
    category: str = '', limit: int = 200, offset: int = 0,
):
    """Per-segment review view: what was translated, what was kept as-is
    (and why), and what failed. Backs the Segments panel in the UI."""
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
        cat = _segment_category(d)
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


# seg_id values are XML paths (e.g. "word/document.xml#table.bt5.r5.c0#p0")
# and contain literal '/' — the default {seg_id} single-segment matcher
# silently fails to match the client's percent-encoded slash, falling
# through to the SPA catch-all (GET-only) and surfacing as a bare 405 on
# every segment. `:path` matches across slashes and fixes that — but it's
# greedy, so this suffixed route (and .../language below) MUST be declared
# before the bare .../segments/{seg_id} route further down, or that route's
# unbounded :path would swallow "<seg_id>/retranslate" whole as its seg_id
# and this one would never be reached (matched 2026-08-24: a 422 complaining
# about a missing `translated_text` field was the tell).
@router.post(
    '/translate/jobs/{job_id}/segments/{seg_id:path}/retranslate', dependencies=[Depends(require_translate)],
)
async def retranslate_segment(job_id: int, seg_id: str, request: Request):
    """Force one segment through the LLM again, regardless of how the audit
    classified it — the fix for a segment wrongly kept as-is (e.g. a false
    "this is already the target language" detection): the reviewer doesn't
    have to know/type the correct translation themselves, just say "this
    needs translating". Deliberately calls _translate_batch directly (not
    _translate_unique_strings, which flips the job's overall status to
    'translating' as a side effect) so fixing one segment on an otherwise
    done job doesn't perturb its status."""
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

    # A bilingual-inline segment holds BOTH languages: only its source side may
    # go to the LLM, and the stored text is composed exactly as the translation
    # stage does. Sending the whole text translated the kept side too, and for a
    # format split the full text was spliced into the source side's runs only,
    # duplicating the kept side in the document.
    plan = None
    if seg['pattern_type'] in _INLINE_PATTERNS:
        try:
            inline = json.loads(seg['inline_split']) if seg['inline_split'] else None
        except (TypeError, json.JSONDecodeError):
            inline = None
        plan = _plan_segment_translation(
            {'pattern_type': seg['pattern_type'], 'source_text': seg['source_text'], 'inline_split': inline,
             'detected_lang': job['source_lang'], 'lang_confidence': None, 'out_of_page_range': False},
            job['source_lang'], job['target_lang'], 'monolingual',
        )
    query = plan['query'] if plan else seg['source_text']

    endpoint = os.getenv('TRANSLATE_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''))
    host, token = _get_llm_credentials()
    if not (endpoint and host and token):
        return JSONResponse({'error': 'Translation endpoint not configured'}, status_code=503)

    glossary_rows = await _get_glossary_rows()
    try:
        result = await _translate_batch(
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
async def update_segment_language(job_id: int, seg_id: str, body: SegmentLanguageIn, request: Request):
    """Manual language override — a reviewer correcting a mis-detected
    language WITHOUT necessarily wanting an LLM call right now (e.g. marking
    it correctly as a third language that's neither source nor target).
    Doesn't touch translated_text/keep_as_is; the reviewer uses the
    "Translate" action (POST .../retranslate) separately when the corrected
    language is the source language and the segment needs translating."""
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
async def dismiss_segment_warning(job_id: int, seg_id: str, request: Request):
    """Reviewer says "this is fine as-is" (typically a proper noun/surname
    the residual-language check keeps flagging, e.g. "HANSLIK") without
    forcing a translation onto it. The next rebuild's residual-language list
    excludes this seg_id permanently (see _run_rebuild_stage) — doesn't
    itself trigger a rebuild, the dismissal just takes effect on the next one."""
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


# Declared last among the /segments/{seg_id}... routes: its unsuffixed
# :path would otherwise greedily swallow the /retranslate and /language
# routes' paths too — see the comment above retranslate_segment.
@router.patch(
    '/translate/jobs/{job_id}/segments/{seg_id:path}', dependencies=[Depends(require_translate)],
)
async def update_segment_translation(job_id: int, seg_id: str, body: SegmentTranslationIn, request: Request):
    """Manually correct one segment's translation — a reviewer must always be
    able to override the LLM, at any stage (including after the job is
    'done'). Marks the segment as human-confirmed (keep_as_is=False so
    rebuild treats it as translated content, conflict_flag=False since a
    human just resolved whatever the flag was about) — does NOT rebuild the
    .docx itself; the reviewer re-runs POST .../rebuild afterwards (works
    from 'done' too, see start_rebuild) to bake the edit into the output."""
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


class AnswerItem(BaseModel):
    q_id: str
    answer: str


class AnswerRequest(BaseModel):
    answers: List[AnswerItem]


@router.post('/translate/jobs/{job_id}/answer', dependencies=[Depends(require_translate)])
async def answer_translation_questions(job_id: int, body: AnswerRequest, request: Request):
    """Record answers to a job's batched clarifying questions.

    These questions (which side to replace, DNT-token confirmations,
    conflicts, low-confidence detections) are deterministically generated by
    audit.py and answered by a human reviewer — no LLM call is involved here;
    call_llm_json is reserved for the batched translation stage.
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
        await _update_job(job_id, status='answered')

    return {'remaining_questions': remaining}


# ---------------------------------------------------------------------------
# Answer application — turn reviewer answers into segment/DNT updates
# ---------------------------------------------------------------------------

_LANG_ALIASES = {
    'en': ('en', 'english', 'anglais'),
    'fr': ('fr', 'french', 'français', 'francais'),
    'es': ('es', 'spanish', 'espagnol'),
    'de': ('de', 'german', 'allemand'),
    'cs': ('cs', 'czech', 'tchèque', 'tcheque'),
    'bg': ('bg', 'bulgarian', 'bulgare'),
    'pt': ('pt', 'portuguese', 'portugais'),
    'ar': ('ar', 'arabic', 'arabe'),
}

def _parse_lang_answer(answer: str) -> Optional[str]:
    """Deterministically extract exactly one supported language from a free-text
    answer ('c'est du tchèque', 'CS', 'czech'). None when ambiguous or absent."""
    words = set(re.findall(r'[a-zà-ÿ]+', (answer or '').lower()))
    matches = {code for code, names in _LANG_ALIASES.items() if words & set(names)}
    return matches.pop() if len(matches) == 1 else None


async def _apply_answers(job_id: int) -> None:
    """Apply reviewer answers before translation runs.

    Deterministic categories only: 'unknown_lang' answers fix detected_lang on
    their segments (changing whether they get translated). Free-text
    'conflict' answers stay informational — conflicting segments are already
    flagged for review.

    No 'dnt_confirm' category exists anymore — dates/part numbers/standards
    are recognized generically by DNT_REGEX (langdetect.py) and by whatever
    custom dnt_rules a reviewer has curated by hand in the glossary panel,
    both applied live during audit (see DntMatcher wiring in
    services/processors/translation.py::audit_segments). Per-job auto-confirm
    used to insert one exact-match dnt_rules row per literal token seen
    (a date, a PN...) which almost never recurred verbatim in a later job —
    it only bloated dnt_rules without changing any translation outcome.
    """
    pool = get_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        q_rows = await conn.fetch(
            '''
            SELECT q_id, category, seg_ids, context, answer
            FROM translation_questions
            WHERE job_id = $1 AND answer IS NOT NULL AND answer != ''
            ''',
            job_id,
        )
        for q in q_rows:
            if q['category'] == 'unknown_lang':
                lang = _parse_lang_answer(q['answer'])
                seg_ids = json.loads(q['seg_ids'] or '[]')
                if lang and seg_ids:
                    await conn.execute(
                        '''
                        UPDATE translation_segments
                        SET detected_lang = $3, lang_confidence = 1.0
                        WHERE job_id = $1 AND seg_id = ANY($2::text[])
                        ''',
                        job_id, seg_ids, lang,
                    )
                    logger.info('translate job %s: %s → detected_lang=%s for %d segment(s)',
                                job_id, q['q_id'], lang, len(seg_ids))


_INLINE_PATTERNS = ('bilingual_inline_slash', 'bilingual_inline_concat',
                    'bilingual_inline_charsplit')


def _split_ws(text: str) -> tuple[str, str, str]:
    """(leading_ws, core, trailing_ws) with text == lead + core + trail."""
    lead = len(text) - len(text.lstrip())
    trail = len(text) - len(text.rstrip())
    return text[:lead], text[lead:len(text) - trail], text[len(text) - trail:] if trail else ''


# Keep a whole mono segment untranslated (in monolingual mode) only when the
# constrained detector is CONFIDENT (agreement-backed, see audit.CONFIDENT) it
# is a language OTHER than the source AND the segment is long enough that the
# label is trustworthy. The kept language is whatever is NOT the source —
# usually a third language (English part-numbers/labels), not the declared
# target. Short confident-but-maybe-wrong labels (a cognate title) fall below
# this and get translated instead of silently kept.
_KEEP_OTHER_CONF = 0.85
_KEEP_OTHER_MIN_WORDS = 5


def _plan_segment_translation(row: Dict[str, Any], source_lang: str,
                              target_lang: str, mode: str) -> Optional[Dict[str, Any]]:
    """Decide what one segment row sends to the LLM, or None to keep as is.

    The user's declared source_lang/target_lang and the document `mode` drive
    the whole-segment decision (the fragile per-segment auto-detection is no
    longer the sole arbiter):
      - monolingual: translate every mono segment EXCEPT a confidently-detected
        target-language one (a genuine other-language passage, e.g. an English
        confidentiality boilerplate in a French doc). This is what stops the
        silent drops — a segment the detector merely guessed wrong is still
        translated, not left as-is.
      - bilingual: translate the source-language side only (keep the other),
        the classic replace-one-side behaviour.

    Bilingual-inline segments (slash / format / charsplit) hold BOTH
    languages in one segment: only the source-language side goes to the LLM
    and the other side must survive verbatim. Sending the whole text (the
    pre-2026-07-17 behaviour) either translated the kept side too, or —
    when the whole-text language label wasn't source_lang — silently skipped
    the segment entirely (client/src/components/translate/README.md "Known issue").

    Returns {'query', 'compose', 'kept_text', 'inline_json'}:
      - query: the exact string to translate (deduped across segments)
      - compose(translated) -> the value stored in translated_text — the
        FULL replacement text for whole-segment and char/slash splits
        (rebuild replaces uniformly), or the bare span for format splits
        (rebuild splices it into the matching-format runs only)
      - kept_text: verbatim-preserved text NOT present in the stored value
        (format splits only) — so the DNT post-check doesn't flag tokens
        that live on the kept side
      - inline_json: updated inline_split JSON to persist (format splits
        mark span_translated/source_fmt for the rebuild/fit-check dispatch)
    """
    pattern = row['pattern_type']
    if pattern in ('dnt', 'numeric_only') or row.get('out_of_page_range'):
        return None
    text = row['source_text']
    inline = row.get('inline_split')
    if pattern in _INLINE_PATTERNS and isinstance(inline, dict):
        kind = inline.get('kind')
        if kind in ('char', 'slash'):
            off = inline.get('offset')
            sep_len = 1 if kind == 'slash' else 0
            if (isinstance(off, int) and 0 < off < len(text)
                    and (kind != 'slash' or text[off] == '/')):
                left_raw, sep, right_raw = text[:off], text[off:off + sep_len], text[off + sep_len:]
                for side_raw, lang, is_left in ((left_raw, inline.get('left_lang'), True),
                                                (right_raw, inline.get('right_lang'), False)):
                    if lang != source_lang:
                        continue
                    lead, core, trail = _split_ws(side_raw)
                    if not core:
                        break
                    if is_left:
                        compose = (lambda tr, _l=lead, _t=trail, _s=sep, _o=right_raw:
                                   f'{_l}{tr}{_t}{_s}{_o}')
                    else:
                        compose = (lambda tr, _l=lead, _t=trail, _s=sep, _o=left_raw:
                                   f'{_o}{_s}{_l}{tr}{_t}')
                    return {'query': core, 'compose': compose, 'kept_text': '', 'inline_json': None}
                return None  # neither side is the source language: keep whole
            # Malformed/drifted split metadata: whole-text fallback below.
        elif kind == 'format':
            text_a, text_b = inline.get('text_a'), inline.get('text_b')
            if isinstance(text_a, str) and isinstance(text_b, str):
                for span, lang, fmt, kept in ((text_a, inline.get('lang_a'), inline.get('fmt_a'), text_b),
                                              (text_b, inline.get('lang_b'), inline.get('fmt_b'), text_a)):
                    if lang != source_lang:
                        continue
                    core = span.strip()
                    if not core:
                        break
                    updated = dict(inline)
                    updated.update({'span_translated': True,
                                    'source_fmt': fmt or '',
                                    'source_span_len': len(span)})
                    return {'query': core, 'compose': (lambda tr: tr),
                            'kept_text': kept, 'inline_json': json.dumps(updated)}
                return None
            # Rows audited before side texts were recorded: fallback below.
    if mode == 'monolingual':
        # Translate everything except a confidently-detected OTHER-language
        # passage (any language that isn't the source — usually English, not
        # the declared target). Undetermined / low-confidence / source-language
        # all get translated — no silent keep on a guessed label.
        conf = row.get('lang_confidence') or 0.0
        dl = row['detected_lang']
        if (dl not in (source_lang, '??', None) and conf >= _KEEP_OTHER_CONF
                and len(text.split()) >= _KEEP_OTHER_MIN_WORDS):
            return None
        return {'query': text, 'compose': (lambda tr: tr), 'kept_text': '', 'inline_json': None}
    # bilingual: source-language side only
    if row['detected_lang'] == source_lang:
        return {'query': text, 'compose': (lambda tr: tr), 'kept_text': '', 'inline_json': None}
    return None


_OWN_FLAG_SQL = (
    "COALESCE(conflict_detail, '') LIKE 'Translation failed%' "
    "OR COALESCE(conflict_detail, '') LIKE 'Translation looks%' "
    "OR COALESCE(conflict_detail, '') LIKE 'DNT token(s) lost%'"
)


def _length_anomaly(source: str, translated: str) -> Optional[str]:
    """A translation far shorter than a long source is a dropped sentence; far
    longer, text the model added. Neither is caught anywhere else: the residual
    check only looks for leftover source words, so a half-translated paragraph
    read as finished. Wide margins (languages differ by ~±40%) keep this to the
    cases that are wrong in any language pair."""
    n_src, n_tr = len(source.strip()), len(translated.strip())
    if n_src >= 60 and n_tr < 0.3 * n_src:
        return 'Translation looks incomplete (much shorter than the source)'
    if n_src >= 40 and n_tr > 3 * n_src:
        return 'Translation looks too long (text may have been added)'
    return None


async def _apply_resolved_segment(
    conn, seg_row: Dict[str, Any], plan: Dict[str, Any], translated_span: str,
    by_source: Dict[str, Dict[str, List[int]]], source_lang: str,
) -> None:
    """Compose + DNT-check one resolved (query -> translation) result into its
    segment row and write it immediately. Shared by the per-batch incremental
    write path and used once per newly-resolved query — see
    _run_translation_stage's on_resolved callback."""
    translated = plan['compose'](translated_span)
    # DNT post-check: part numbers / standards / codes must survive
    # translation verbatim — flag for review if not. kept_text covers the
    # side of a format-split segment that stays in its own runs (not in
    # translated_text).
    try:
        tokens = json.loads(seg_row['dnt_tokens'] or '[]')
    except (TypeError, json.JSONDecodeError):
        tokens = []
    translated_norm, kept_norm = normalize_hyphens(translated), normalize_hyphens(plan['kept_text'])
    lost_tokens = [t for t in tokens if t not in translated_norm and t not in kept_norm]
    dnt_lost = bool(lost_tokens)
    # conflict_flag alone used to reach this point with no conflict_detail —
    # generate_review_comments requires BOTH to emit a Word comment, so a
    # mangled part number/standard reference (exactly what this check exists
    # to catch) silently got no comment at all, even though it's visibly
    # flagged in the Segments panel. COALESCE keeps any detail audit.py
    # already set rather than overwriting it.
    dnt_detail = f"DNT token(s) lost in translation: {', '.join(lost_tokens)}" if dnt_lost else None
    length_detail = _length_anomaly(plan['query'], translated_span)
    dnt_detail = dnt_detail or length_detail
    dnt_lost = dnt_lost or bool(length_detail)
    # A segment reaching this point was, by definition, planned for
    # translation — i.e. its true language is the document's source
    # language, whatever the audit's (possibly wrong) per-segment guess had
    # said. Correct detected_lang here too, or the Lang column keeps
    # advertising a stale/wrong detection on an otherwise well-translated row.
    await conn.execute(
        # A flag this function (or the failure path) left on the PREVIOUS
        # translation of the segment describes a state that no longer exists.
        'UPDATE translation_segments SET translated_text = $2, '
        f"conflict_flag = (conflict_flag AND NOT ({_OWN_FLAG_SQL})) OR $3, "
        f"conflict_detail = CASE WHEN {_OWN_FLAG_SQL} THEN $5 "
        'ELSE COALESCE(conflict_detail, $5) END, '
        'inline_split = COALESCE($4, inline_split), '
        'detected_lang = $6, lang_confidence = NULL WHERE id = $1',
        seg_row['id'], translated, dnt_lost, plan['inline_json'], dnt_detail, source_lang,
    )
    by_source.setdefault(seg_row['source_text'], {}).setdefault(translated, []).append(seg_row['id'])


async def _run_translation_stage(job_id: int, source_lang: str, target_lang: str) -> None:
    pool = get_pool()
    # Held only while this stage actively runs, released on completion/failure
    # below — see _active_jobs_semaphore's definition.
    await _active_jobs_semaphore.acquire()
    beat = _spawn(_heartbeat_loop(job_id))
    try:
        endpoint = os.getenv('TRANSLATE_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''))
        if not endpoint:
            raise RuntimeError('TRANSLATE_ENDPOINT (or COMPARE_ANALYSIS_ENDPOINT) not configured')
        host, token = _get_llm_credentials()
        if not host or not token:
            raise RuntimeError('Missing Databricks credentials for translation endpoint')

        # Reviewer answers can change detected_lang / DNT rules — apply them
        # before deciding what gets translated.
        await _apply_answers(job_id)

        async with pool.acquire() as conn:
            seg_rows = await conn.fetch(
                'SELECT id, source_text, pattern_type, detected_lang, lang_confidence, '
                'dnt_tokens, inline_split, translated_text, out_of_page_range '
                'FROM translation_segments WHERE job_id = $1 ORDER BY id',
                job_id,
            )

        # Recompute the document mode from the persisted per-segment detections
        # (robust to stage_progress being overwritten by later stages).
        mode = _determine_mode([dict(r) for r in seg_rows], source_lang, target_lang)

        seg_row_by_id: Dict[int, Dict[str, Any]] = {}
        plans: Dict[int, Dict[str, Any]] = {}
        for r in seg_rows:
            row = dict(r)
            try:
                row['inline_split'] = json.loads(r['inline_split']) if r['inline_split'] else None
            except (TypeError, json.JSONDecodeError):
                row['inline_split'] = None
            seg_row_by_id[r['id']] = row
            # Already resolved by a prior (crashed/retried) run of this stage —
            # skip re-translating it. This is what makes the stage resumable:
            # translated_text is written per-batch below, not only at the end.
            if row['translated_text'] is not None:
                continue
            plan = _plan_segment_translation(row, source_lang, target_lang, mode)
            if plan:
                plans[r['id']] = plan

        async with pool.acquire() as conn:
            # Everything NOT planned for translation (kept-side language,
            # DNT, numeric-only, bilingual-inline rows with no source-language
            # side) keeps its source text untouched at rebuild. Without this
            # flag those rows carry translated_text=NULL and crash the
            # fit-check stage. Restricted to still-unresolved rows so a resumed
            # run doesn't flip an already-translated row's keep_as_is back on.
            await conn.execute(
                '''
                UPDATE translation_segments
                SET keep_as_is = NOT (id = ANY($2::bigint[]))
                WHERE job_id = $1 AND translated_text IS NULL
                ''',
                job_id, list(plans.keys()),
            )
        # First-occurrence order (not sorted): keeps neighbouring segments in
        # the same LLM batch, which helps terminology consistency.
        unique_strings = list(dict.fromkeys(p['query'] for p in plans.values()))
        query_to_seg_ids: Dict[str, List[int]] = {}
        for seg_id, plan in plans.items():
            query_to_seg_ids.setdefault(plan['query'], []).append(seg_id)

        already_resolved = sum(1 for r in seg_row_by_id.values() if r['translated_text'] is not None)
        await _update_job(
            job_id, status='translating',
            stage_progress={
                'stage': 'translating',
                'done': already_resolved,
                'total': len(unique_strings) + already_resolved,
            },
        )

        # Tracks (source_text -> {translated_text -> [seg_id, ...]}) for the
        # post-loop consistency check below — two segments with the exact
        # same source_text should never end up with a different translation.
        by_source: Dict[str, Dict[str, List[int]]] = {}

        async def _on_batch_resolved(resolved: Dict[str, str]) -> None:
            # Committed per batch (own short transaction) rather than waiting
            # for the whole job to finish translating — a crash/restart after
            # this point keeps this batch's work instead of losing it (see
            # the translated_text IS NULL resume filter above).
            async with pool.acquire() as conn:
                async with conn.transaction():
                    for query, translated_span in resolved.items():
                        for seg_id in query_to_seg_ids.get(query, []):
                            await _apply_resolved_segment(
                                conn, seg_row_by_id[seg_id], plans[seg_id], translated_span, by_source, source_lang,
                            )

        if unique_strings:
            glossary_rows = await _get_glossary_rows()
            translations, failed = await _translate_unique_strings(
                host, token, endpoint, unique_strings, source_lang, target_lang, glossary_rows, job_id,
                on_resolved=_on_batch_resolved,
                progress_offset=already_resolved, progress_total=already_resolved + len(unique_strings),
            )
        else:
            translations, failed = {}, []

        if failed:
            # Unresolved after every retry: keep the source text at rebuild
            # and flag for manual review.
            failed_seg_ids = [seg_id for seg_id, plan in plans.items() if plan['query'] in failed]
            async with pool.acquire() as conn:
                await conn.execute(
                    'UPDATE translation_segments SET conflict_flag = TRUE, keep_as_is = TRUE, '
                    "conflict_detail = COALESCE(conflict_detail, 'Translation failed after all retries — use the translate button') "
                    'WHERE id = ANY($1::bigint[])',
                    failed_seg_ids,
                )

        # Deterministic consistency guard: the same exact source_text should
        # never resolve to two different translations within one job (can
        # happen across separately-composed inline splits even though the
        # underlying `query` was deduped). Flag for review, never auto-fix.
        inconsistent_ids = [
            seg_id
            for variants in by_source.values() if len(variants) > 1
            for seg_id in [i for ids in variants.values() for i in ids]
        ]
        if inconsistent_ids:
            async with pool.acquire() as conn:
                await conn.execute(
                    '''
                    UPDATE translation_segments
                    SET conflict_flag = TRUE,
                        conflict_detail = COALESCE(conflict_detail, 'Same source text translated differently elsewhere in this document')
                    WHERE id = ANY($1::bigint[])
                    ''',
                    inconsistent_ids,
                )
            logger.warning('translate job %s: %d segment(s) flagged for inconsistent terminology',
                            job_id, len(inconsistent_ids))

        await _update_job(
            job_id, status='translated',
            stage_progress={
                'stage': 'translated',
                'mode': mode,
                'unique_strings': len(unique_strings),
                'translated_count': len(translations),
                'failed_count': len(failed),
            },
        )
        if failed:
            logger.warning('translate job %s: %d/%d unique strings failed translation after retries',
                            job_id, len(failed), len(unique_strings))
        logger.info('translate job %s: translation stage complete (%d/%d unique strings)',
                    job_id, len(translations), len(unique_strings))
    except Exception as e:
        logger.error('translate job %s: translation stage failed: %s', job_id, e, exc_info=True)
        await _update_job(job_id, status='failed', error_type=type(e).__name__, error_msg=str(e)[:2000])
    finally:
        beat.cancel()
        _active_jobs_semaphore.release()


@router.post('/translate/jobs/{job_id}/translate', dependencies=[Depends(require_translate)])
async def start_translation(job_id: int, request: Request):
    """Trigger the batched translation stage — kept as an explicit step
    (rather than auto-chaining after /answer) so the UI can show a review
    step between answering questions and running the (potentially
    multi-minute) translation."""
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
        # Atomically claim the job: only flip to 'translating' if status is
        # still one of the allowed values at this instant. A single UPDATE's
        # WHERE clause is evaluated against the post-lock row, so a
        # double-click or a second browser tab racing this same request can
        # win the SELECT check above but still lose here — closing the
        # check-then-act window that let two concurrent translation runs
        # start on the same job.
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

    _spawn(_run_translation_stage(job_id, job['source_lang'], job['target_lang']))
    return {'status': 'translating'}


async def _download_from_volume(path: str) -> bytes:
    return await asyncio.to_thread(_storage.download, path)


def _preview_pdf_paths(output_volume_path: str) -> tuple[str, str]:
    """Volume paths of the pregenerated preview PDFs, next to output.docx."""
    job_dir = output_volume_path.rsplit('/', 1)[0]
    return f'{job_dir}/preview_original.pdf', f'{job_dir}/preview_translated.pdf'


# The previews and the shared view show output.docx: only a finished job's file
# has passed the structural checks (a 'failed' one may be broken) and is not the
# previous rebuild's leftover while a new one runs.
_PREVIEWABLE_STATUSES = ('done', 'done_with_warnings')


async def _discard_preview_pdfs(job_id: int) -> None:
    """Delete the pregenerated previews before a rebuild/restart: until the new
    ones are written (several seconds after 'done') the preview endpoints would
    serve the previous build's pages next to the new document."""
    for path in _preview_pdf_paths(f'{_storage.job_root()}/{job_id}/output.docx'):
        try:
            await asyncio.to_thread(_storage.delete, path)
        except Exception as e:
            logger.warning('translate job %s: could not delete stale preview %s: %s', job_id, path, e)


def _revalidating_response(content: bytes, media_type: str, request: Request,
                           extra_headers: Optional[Dict[str, str]] = None) -> Response:
    """Always revalidated (ETag), never served from the browser's cache blindly:
    max-age=600 kept showing the pre-rebuild PDF in the iframe for ten minutes
    after 'Rebuild again'."""
    etag = '"' + hashlib.sha1(content).hexdigest() + '"'
    headers = {'Cache-Control': 'private, no-cache', 'ETag': etag, **(extra_headers or {})}
    if request.headers.get('if-none-match') == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=content, media_type=media_type, headers=headers)


async def _generate_preview_pdfs(
    job_id: int, src_bytes: bytes, rebuilt_bytes: bytes, output_volume_path: str,
) -> None:
    """Convert both sides to PDF and persist them in the job's volume folder.

    Runs fire-and-forget right after a successful rebuild (and lazily after
    the first preview of an older job), so opening the preview is two plain
    file downloads instead of two LibreOffice conversions — effectively
    instant, and it survives app restarts (the local conversion cache does
    not). Best-effort: any failure just means the preview endpoint falls
    back to converting on demand.
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
    before_path, after_path = _preview_pdf_paths(output_volume_path)
    try:
        await asyncio.to_thread(_storage.upload, before_path, before_pdf)
        await asyncio.to_thread(_storage.upload, after_path, after_pdf)
        logger.info('translate job %s: preview PDFs persisted to volume', job_id)
    except Exception as e:
        logger.warning('translate job %s: preview PDF volume upload failed: %s', job_id, e)


async def _propose_glossary_candidates(
    job_id: int, source_lang: str, target_lang: str,
) -> None:
    """Fire-and-forget, best-effort: proposes glossary_candidates rows from
    a job's own confirmed translations (server/services/translation/glossary_extract.py).
    Never blocks or fails the job. Candidates land at status='pending', same
    human-review gate as everything else in the "To review" tab; this is NOT
    the LLM-auto-verify/auto-publish path built for the one-time Intraqual
    corpus bootstrap (utils/glossary/sync_candidates_to_lakebase.py) — that
    stays scoped to that bootstrap only.

    Only called from the /validate endpoint — a reviewer explicitly signing
    off on the whole document is the gate, not job completion, so an
    unreviewed rebuild never leaks terms into the candidate queue.
    """
    try:
        pool = get_pool()
        if not pool:
            return
        async with pool.acquire() as conn:
            seg_rows = await conn.fetch(
                '''
                SELECT source_text, translated_text FROM translation_segments
                WHERE job_id = $1 AND pattern_type = 'mono' AND NOT keep_as_is
                ''',
                job_id,
            )
        pairs = [
            (r['source_text'], r['translated_text'])
            for r in seg_rows
            if r['source_text'] and r['translated_text'] and r['source_text'] != r['translated_text']
        ]
        if not pairs:
            return
        endpoint = os.getenv('TRANSLATE_ENDPOINT', os.getenv('COMPARE_ANALYSIS_ENDPOINT', ''))
        host, token = _get_llm_credentials()
        if not endpoint or not host or not token:
            return
        if source_lang not in _LANG_NAMES or target_lang not in _LANG_NAMES:
            return  # extra guard: these become raw column names in the INSERT below

        candidates = await extract_job_candidates(pairs, source_lang, target_lang, host, token, endpoint)
        if not candidates:
            return

        pool = get_pool()
        if not pool:
            return
        async with pool.acquire() as conn:
            job = await conn.fetchrow('SELECT original_filename FROM translation_jobs WHERE id = $1', job_id)
            source_label = (job['original_filename'] if job else None) or f'job {job_id}'

            known = {
                normalize_term(v) for r in await conn.fetch('SELECT en, fr, cs, bg, de, es, pt, ar FROM glossary_terms')
                for v in dict(r).values() if v
            }
            # A previously rejected candidate must not silently reappear as a
            # "new" pending one the next time a job proposes the same pair —
            # block it permanently, same as an already-approved term.
            known |= {
                normalize_term(v)
                for r in await conn.fetch(
                    "SELECT en, fr, cs, bg, de, es, pt, ar FROM glossary_candidates WHERE status = 'rejected'"
                )
                for v in dict(r).values() if v
            }
            existing_cand = [dict(r) for r in await conn.fetch(
                'SELECT id, en, fr, cs, bg, de, es, pt, ar, n_docs, sources FROM glossary_candidates'
                " WHERE status = 'pending'"
            )]

            novel = [c for c in candidates if not any(normalize_term(v) in known for v in c.values())]
            if novel:
                # Same LLM-verification pass the offline corpus-sync pipeline
                # uses (utils/glossary/sync_candidates_to_lakebase.py) — catches
                # extraction artifacts before a candidate reaches human review.
                # Blocking/sync call (plain httpx under the hood) — run off the
                # event loop.
                novel = await asyncio.to_thread(
                    llm_verify, novel, source_lang, target_lang, endpoint, host, token,
                )

            inserted = 0
            for c in novel:
                match = next(
                    (r for r in existing_cand
                     if normalize_term(r[source_lang] or '') == normalize_term(c[source_lang])
                     and normalize_term(r[target_lang] or '') == normalize_term(c[target_lang])),
                    None,
                )
                if match:
                    sources = match['sources'] or ''
                    if source_label not in [s.strip() for s in sources.split(',')]:
                        sources = f'{sources}, {source_label}' if sources else source_label
                        await conn.execute(
                            'UPDATE glossary_candidates SET n_docs = n_docs + 1, sources = $2 WHERE id = $1',
                            match['id'], sources[:500],
                        )
                    continue
                await conn.execute(
                    f'''
                    INSERT INTO glossary_candidates ({source_lang}, {target_lang}, n_docs, sources, status)
                    VALUES ($1, $2, 1, $3, 'pending')
                    ''',
                    c[source_lang], c[target_lang], source_label,
                )
                inserted += 1
                known.add(normalize_term(c[source_lang]))
                known.add(normalize_term(c[target_lang]))
        if inserted:
            logger.info('translate job %s: proposed %d new glossary candidate(s)', job_id, inserted)
    except Exception as e:
        logger.warning('translate job %s: glossary candidate proposal failed: %s', job_id, e)


async def _run_rebuild_stage(job_id: int, source_lang: str, include_review_comments: bool = False) -> None:
    pool = get_pool()
    # Held only while this stage actively runs, released on completion/failure
    # below — see _active_jobs_semaphore's definition.
    await _active_jobs_semaphore.acquire()
    beat = _spawn(_heartbeat_loop(job_id))
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

        await _discard_preview_pdfs(job_id)
        await _update_job(job_id, status='fit_checking')
        src_bytes = await _download_from_volume(job['input_volume_path'])
        extract_segments = await asyncio.to_thread(extract_docx_segments, src_bytes)

        def _json_or_none(value):
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

            # Fill part/XML paths from the fresh extraction when the DB row lacks
            # them: the source document is the ground truth for positions, and
            # rows written before audit_segments carried these fields (or after a
            # re-upload) stay rebuildable.
            ext_by_id = {s['seg_id']: s for s in extract_segments}
            combined = []
            missing_translation_ids: List[str] = []
            # docx_image_ocr segments (see docx_images.py) have no real XML
            # position — xml_choice_path holds {"media_filename": ...} instead
            # of a positional path — never passed to the XML-position-based
            # rebuild functions. Their translation is surfaced purely in the
            # app's Segments panel (an "Images" category), never written into
            # the document itself: placement inside the docx flow (right after
            # the image's own paragraph) turned out unreliable on documents
            # where images sit in headers/footers/text boxes, landing the
            # translation far from the image instead of right after it.
            for r in seg_rows:
                if r['location_type'] == 'docx_image_ocr':
                    continue
                ext = ext_by_id.get(r['seg_id'], {})
                # A segment planned for translation that has none (a write lost
                # mid-stage) used to crash the whole rebuild on a None text.
                # It keeps its source text and is flagged instead.
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

            await _update_job(job_id, status='rebuilding')
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
                    d['category'] = _segment_category(d)
                    segments_for_review.append(d)
                review_comments = generate_review_comments(segments_for_review, flagged)
                if review_comments:
                    xml_choice_paths = {c['seg_id']: c['xml_choice_path'] for c in combined if c.get('xml_choice_path')}
                    rebuilt_bytes, comments_placed = await asyncio.to_thread(
                        inject_comments, rebuilt_bytes, review_comments, xml_choice_paths,
                    )
                    logger.info('translate job %s: injected %d/%d review comment(s)',
                                job_id, comments_placed, len(review_comments))

            await _update_job(job_id, status='validating')
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
            # A reviewer-dismissed segment (e.g. a proper noun the check keeps
            # flagging) never counts against this job again — neither for the
            # auto-retry pass below nor for the warning list shown after.
            # DNT / numeric-only / out-of-page-range segments stay in the source
            # language by design — flagging them made every page-filtered job
            # report its whole untouched range, and the re-pass below then
            # translated those pages anyway.
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
            host, token = _get_llm_credentials()
            if not (endpoint and host and token):
                break  # can't retry without LLM credentials — surface as-is

            # Re-plan through _plan_segment_translation (forcing the source
            # language, which is what the residual check just established) so a
            # bilingual-inline segment re-sends only its source side: sending the
            # whole source_text translated the kept side too, and for format
            # splits got the full text spliced into the span's runs.
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
                plan = _plan_segment_translation(row, source_lang, job['target_lang'], 'monolingual')
                if plan:
                    retry_plans[it['seg_id']] = {'row': row, 'plan': plan}
            unique_strings = list(dict.fromkeys(p['plan']['query'] for p in retry_plans.values()))
            if not unique_strings:
                break
            already_retried = True
            logger.info('translate job %s: %d segment(s) still read as %s — running one automatic re-translation pass',
                        job_id, len(retry_plans), source_lang.upper())
            glossary_rows = await _get_glossary_rows()
            retried, _ = await _translate_unique_strings(
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
                        await _apply_resolved_segment(conn, p['row'], p['plan'], new_span, by_source, source_lang)
                        await conn.execute(
                            'UPDATE translation_segments SET keep_as_is = FALSE WHERE id = $1', p['row']['id'],
                        )
            # Loop again: refetch seg_rows (now carrying the re-translated
            # text) and redo fit_check → rebuild → validate on top of it.

        output_path = f'{_storage.job_root()}/{job_id}/output.docx'
        try:
            await asyncio.to_thread(_storage.upload, output_path, rebuilt_bytes)
        except Exception as e:
            logger.warning('translate job %s: failed to upload output to volume: %s', job_id, e)
            output_path = None

        # Residual-source-language and fit_check CRITICAL segments were only
        # ever surfaced via stage_progress (the job status page's "Review &
        # fix" panel, which jumps into the Segments panel with an explicit
        # seg_id list) — the Segments panel's own "Flagged" tab filters on
        # conflict_flag, which neither of these ever set, so it showed 0
        # regardless of what the job status page reported. Persist both here
        # so the tab count and the status page agree.
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
        # These flags describe THIS rebuild's output: one left by an earlier
        # rebuild kept showing "Still reads as BG" on a segment fixed since.
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
        await _update_job(
            job_id, status=final_status,
            output_volume_path=output_path,
            error_type=None if structural_ok else 'ValidationFailed',
            error_msg=None if structural_ok else json.dumps(failed_checks, ensure_ascii=False)[:2000],
            stage_progress={
                'stage': final_status,
                'fit_check_flagged': len(flagged),
                'fit_check_critical': critical,
                # seg_ids so the UI can jump straight to the offending segments
                # (see StageDetails' "Review & fix") instead of just a count
                # with no way to find them.
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
            _spawn(_generate_preview_pdfs(job_id, src_bytes, rebuilt_bytes, output_path))
        # Glossary candidates are proposed only once a reviewer explicitly
        # validates the document (POST /translate/jobs/{id}/validate) — never
        # automatically here, however the job turned out.
    except Exception as e:
        logger.error('translate job %s: rebuild stage failed: %s', job_id, e, exc_info=True)
        await _update_job(job_id, status='failed', error_type=type(e).__name__, error_msg=str(e)[:2000])
    finally:
        beat.cancel()
        _active_jobs_semaphore.release()


@router.post('/translate/jobs/{job_id}/rebuild', dependencies=[Depends(require_translate)])
async def start_rebuild(job_id: int, request: Request, include_review_comments: bool = False):
    """Trigger fit_check -> length_adapt -> rebuild -> validate. Requires the
    original document to have been persisted to TRANSLATE_VOLUME_PATH at
    upload time (see _upload_input_to_volume).

    include_review_comments: opt-in — anchors real Word comments (Review
    pane) on flagged/conflicting/failed segments in the output .docx (see
    review_comments.py + comments.py). Off by default since it changes the
    deliverable file; a reviewer who wants the annotated version asks for it
    explicitly."""
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
            # Every segment planned for translation (keep_as_is = FALSE) must
            # actually have translated_text. Checking mere EXISTENCE of one
            # translated-or-kept row (the old query) let a job through whose
            # translation stage crashed after the bulk keep_as_is update but
            # before any planned segment's translated_text got committed —
            # rebuild then crashed on a None translated_text instead of this
            # returning a clear "still needs translation" error.
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
        # Atomically claim the job: only flip to 'fit_checking' if status is
        # still one of the allowed values at this instant — closes the
        # check-then-act race where a double-click/second tab reads the same
        # pre-transition status and both would otherwise start a concurrent
        # rebuild on the same job (two runs writing output.docx at once).
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

    _spawn(_run_rebuild_stage(job_id, job['source_lang'], include_review_comments))
    return {'status': 'fit_checking'}


@router.get('/translate/jobs/{job_id}/preview', dependencies=[Depends(require_translate)])
async def get_translation_preview(job_id: int, request: Request, format: str = 'docx'):
    """Before/after document bytes (base64) for the side-by-side view, once
    the job has been rebuilt.

    ``format=docx`` (default) returns the raw .docx bytes, rendered
    client-side via docx-preview (React) — good fidelity, zero server deps.

    ``format=pdf`` converts both sides through headless LibreOffice
    (server/services/soffice.py) for an *exact* page-layout rendering — the
    same engine fidelity the CLI prototype got from Word COM + PyMuPDF, minus
    the Word dependency Databricks Apps can't satisfy. Returns 501 when no
    LibreOffice engine is available so the client can fall back to docx."""
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
            or job['status'] not in _PREVIEWABLE_STATUSES):
        return JSONResponse(
            {'error': 'Preview not available yet (job not rebuilt, or output storage failed)'},
            status_code=409,
        )

    if format == 'pdf':
        # Fast path: PDFs pregenerated at the end of the rebuild stage
        # (_generate_preview_pdfs) — two plain downloads, no conversion, no
        # LibreOffice needed (a cold app doesn't even extract the engine).
        before_path, after_path = _preview_pdf_paths(job['output_volume_path'])
        try:
            before_pdf, after_pdf = await asyncio.gather(
                _download_from_volume(before_path),
                _download_from_volume(after_path),
            )
            return {
                'before_pdf_base64': base64.b64encode(before_pdf).decode('ascii'),
                'after_pdf_base64': base64.b64encode(after_pdf).decode('ascii'),
                'pregenerated': True,
            }
        except Exception:
            pass  # not pregenerated (older job) — convert on demand below

        try:
            original_bytes = await _download_from_volume(job['input_volume_path'])
            output_bytes = await _download_from_volume(job['output_volume_path'])
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
        _spawn(
            _generate_preview_pdfs(job_id, original_bytes, output_bytes, job['output_volume_path'])
        )
        return {
            'before_pdf_base64': base64.b64encode(before_pdf).decode('ascii'),
            'after_pdf_base64': base64.b64encode(after_pdf).decode('ascii'),
        }

    try:
        original_bytes = await _download_from_volume(job['input_volume_path'])
        output_bytes = await _download_from_volume(job['output_volume_path'])
    except Exception as e:
        logger.error('translate job %s: failed to load documents for preview: %s', job_id, e)
        return JSONResponse({'error': f'Failed to load documents: {e}'}, status_code=500)
    return {
        'before_docx_base64': base64.b64encode(original_bytes).decode('ascii'),
        'after_docx_base64': base64.b64encode(output_bytes).decode('ascii'),
    }


@router.get('/translate/jobs/{job_id}/preview.pdf', dependencies=[Depends(require_translate)])
async def get_translation_preview_pdf(job_id: int, request: Request, side: str = 'before'):
    """One side of the exact preview as raw PDF bytes — used directly as an
    iframe src, so the two panes stream in parallel with no base64 overhead
    and repeat opens hit the browser cache."""
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
            or job['status'] not in _PREVIEWABLE_STATUSES):
        return JSONResponse({'error': 'Preview not available yet'}, status_code=409)

    pregen_path = _preview_pdf_paths(job['output_volume_path'])[0 if side == 'before' else 1]
    try:
        pdf = await _download_from_volume(pregen_path)
        return _revalidating_response(pdf, 'application/pdf', request)
    except Exception:
        pass  # not pregenerated yet — convert this side on demand

    docx_path = job['input_volume_path'] if side == 'before' else job['output_volume_path']
    try:
        docx_bytes = await _download_from_volume(docx_path)
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
    """Shared lookup for every preview-derived endpoint (diff.png,
    report.html, changes): resolve the job's pregenerated before/after PDFs
    (see _generate_preview_pdfs, fired at rebuild time). Returns
    (job_row, before_pdf, after_pdf, None) on success, or
    (None, None, None, JSONResponse) with the error to return as-is.
    extra_cols: extra comma-prefixed columns to select from translation_jobs
    alongside output_volume_path (e.g. ', original_filename')."""
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
    if not job['output_volume_path'] or job['status'] not in _PREVIEWABLE_STATUSES:
        return None, None, None, JSONResponse({'error': 'Preview not available yet'}, status_code=409)

    before_path, after_path = _preview_pdf_paths(job['output_volume_path'])
    try:
        before_pdf, after_pdf = await asyncio.gather(
            _download_from_volume(before_path), _download_from_volume(after_path),
        )
    except Exception:
        return None, None, None, JSONResponse(
            {'error': 'Preview PDFs not generated yet — open the side-by-side preview first'},
            status_code=409,
        )
    return job, before_pdf, after_pdf, None


@router.get('/translate/jobs/{job_id}/preview/diff.png', dependencies=[Depends(require_translate)])
async def get_translation_preview_diff(job_id: int, request: Request, page: int = 1):
    """Pixel-level page diff (grayscale before + red highlights where the
    after page differs) — see server/services/translation/pdf_diff.py."""
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
async def get_translation_preview_changes(job_id: int, request: Request):
    """Bounding boxes of every detected pixel-level change, across all
    pages, for "jump to next/previous change" navigation in the diff view.
    See server/services/translation/pdf_diff.py::get_all_page_changes."""
    identity = await get_user_identity(request)
    _job, before_pdf, after_pdf, error = await _get_preview_pdfs(job_id, identity)
    if error:
        return error

    changes = await asyncio.to_thread(get_all_page_changes, before_pdf, after_pdf)
    return {'changes': changes}




@router.get('/translate/render-engine', dependencies=[Depends(require_translate)])
async def get_render_engine_status():
    """Diagnostic: is the exact-PDF (LibreOffice) engine usable on this
    deployment? First call may trigger the one-time archive download/extract
    from the UC Volume, so it can take a minute on a cold app."""
    return await asyncio.to_thread(soffice_status)


@router.get('/translate/jobs', dependencies=[Depends(require_translate)])
async def list_translation_jobs(request: Request, limit: int = 20, offset: int = 0):
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


# ---------------------------------------------------------------------------
# Feedback (thumbs up/down + optional comment on a job)
# ---------------------------------------------------------------------------

class FeedbackRequest(BaseModel):
    vote: str          # "up" or "down"
    comment: Optional[str] = None
    job_id: Optional[int] = None


@router.post('/translate/feedback', dependencies=[Depends(require_translate)])
async def submit_feedback(body: FeedbackRequest, request: Request):
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


# ---------------------------------------------------------------------------
# Sharing — read-only link, anyone with the token can view (no ownership
# check on the GET), mirrors Qualibot Chat's share_token pattern.
# ---------------------------------------------------------------------------

@router.post('/translate/jobs/{job_id}/share', dependencies=[Depends(require_translate)])
async def share_translation_job(job_id: int, request: Request):
    """Grant read-only access to this job to anyone with the link.

    Idempotent: returns the existing token if the job was already shared,
    rather than rotating it — a previously distributed link keeps working.
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
async def get_shared_translation_job(token: str):
    """Read-only view of a shared translation job — any authenticated app
    user with the link can view it, not just its owner."""
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
    if job['output_volume_path'] and job['status'] in _PREVIEWABLE_STATUSES:
        try:
            output_bytes = await _download_from_volume(job['output_volume_path'])
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


# ---------------------------------------------------------------------------
# Glossary / DNT CRUD — Lakebase-backed, replaces Translator/glossary/*.csv
# ---------------------------------------------------------------------------

class GlossaryTermIn(BaseModel):
    term_id: Optional[str] = None  # omitted/blank -> server generates T####
    en: str = ''
    fr: str = ''
    cs: str = ''
    bg: str = ''
    de: str = ''
    es: str = ''
    pt: str = ''
    ar: str = ''
    domain: str = ''
    notes: str = ''
    definition: str = ''
    # 'REF#chunk' (extraite d'une section terminologie), 'generated' (LLM),
    # 'cross_lang_pair' (définition authentique corroborée par une paire de
    # traduction confirmée) — vide quand pas de définition.
    definition_source: str = ''


class DntRuleIn(BaseModel):
    pattern: str
    type: str = ''
    match_mode: str = 'exact'
    notes: str = ''


@router.get('/translate/glossary', dependencies=[Depends(require_translate)])
async def list_glossary():
    pool = get_pool()
    if not pool:
        return {'terms': [], 'dnt_rules': [], 'available': False}
    async with pool.acquire() as conn:
        term_rows = await conn.fetch(
            'SELECT term_id, en, fr, cs, bg, de, es, pt, ar, domain, notes, '
            'definition, definition_source FROM glossary_terms ORDER BY term_id'
        )
        dnt_rows = await conn.fetch(
            'SELECT id, pattern, type, match_mode, notes FROM dnt_rules ORDER BY id'
        )
    return {
        'terms': [dict(r) for r in term_rows],
        'dnt_rules': [dict(r) for r in dnt_rows],
        'available': True,
    }


@router.post('/translate/glossary/terms', dependencies=[Depends(require_translate)])
async def upsert_glossary_term(body: GlossaryTermIn):
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)

    term_id = (body.term_id or '').strip()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if not term_id:
                term_id = await generate_term_id(conn)

            await conn.execute(
                '''
                INSERT INTO glossary_terms (term_id, en, fr, cs, bg, de, es, pt, ar, domain, notes,
                                            definition, definition_source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ON CONFLICT (term_id) DO UPDATE SET
                    en = EXCLUDED.en, fr = EXCLUDED.fr, cs = EXCLUDED.cs, bg = EXCLUDED.bg,
                    de = EXCLUDED.de, es = EXCLUDED.es, pt = EXCLUDED.pt, ar = EXCLUDED.ar,
                    domain = EXCLUDED.domain,
                    notes = EXCLUDED.notes, definition = EXCLUDED.definition,
                    definition_source = EXCLUDED.definition_source, updated_at = NOW()
                ''',
                term_id, body.en, body.fr, body.cs, body.bg, body.de, body.es, body.pt, body.ar,
                body.domain, body.notes, body.definition, body.definition_source,
            )
    return {'term_id': term_id}


@router.delete('/translate/glossary/terms/{term_id}', dependencies=[Depends(require_translate)])
async def delete_glossary_term(term_id: str):
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM glossary_terms WHERE term_id = $1', term_id)
    return {'deleted': term_id}


@router.post('/translate/glossary/dnt', dependencies=[Depends(require_translate)])
async def add_dnt_rule(body: DntRuleIn):
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    problem = dnt_rule_problem(body.pattern, body.match_mode)
    if problem:
        return JSONResponse({'error': problem}, status_code=422)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'INSERT INTO dnt_rules (pattern, type, match_mode, notes) VALUES ($1, $2, $3, $4) RETURNING id',
            body.pattern, body.type, body.match_mode, body.notes,
        )
    return {'id': row['id']}


@router.delete('/translate/glossary/dnt/{rule_id}', dependencies=[Depends(require_translate)])
async def delete_dnt_rule(rule_id: int):
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    async with pool.acquire() as conn:
        await conn.execute('DELETE FROM dnt_rules WHERE id = $1', rule_id)
    return {'deleted': rule_id}


# ---------------------------------------------------------------------------
# Glossary candidate review — pending queue populated by
# utils/glossary/sync_candidates_to_lakebase.py (replaces hand-editing the
# `status` column of Translator/glossary/candidates/*.csv). Approving a
# candidate inserts it into glossary_terms (same T#### generation as
# upsert_glossary_term above); rejecting just records a reason.
# ---------------------------------------------------------------------------

class GlossaryCandidateApprove(BaseModel):
    # Optional overrides — the reviewer can correct a language before
    # validating; blank fields fall back to the candidate row's own value.
    en: Optional[str] = None
    fr: Optional[str] = None
    cs: Optional[str] = None
    bg: Optional[str] = None
    de: Optional[str] = None
    es: Optional[str] = None
    pt: Optional[str] = None
    ar: Optional[str] = None
    domain: str = ''
    notes: str = ''


class GlossaryCandidateReject(BaseModel):
    reason: str = ''


@router.get('/translate/glossary/candidates', dependencies=[Depends(require_translate)])
async def list_glossary_candidates(status: str = 'pending', limit: int = 50, offset: int = 0):
    pool = get_pool()
    if not pool:
        return {'candidates': [], 'total': 0, 'available': False}
    limit = max(1, min(limit, 200))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            '''
            SELECT id, en, fr, cs, bg, de, es, pt, ar, n_docs, sources, definition,
                   definition_source, priority, status, reviewed_by, reviewed_at,
                   reject_reason
            FROM glossary_candidates
            WHERE status = $1
            ORDER BY priority ASC NULLS LAST, n_docs DESC, id ASC
            LIMIT $2 OFFSET $3
            ''',
            status, limit, offset,
        )
        total = await conn.fetchval(
            'SELECT COUNT(*) FROM glossary_candidates WHERE status = $1', status
        )
    return {'candidates': [dict(r) for r in rows], 'total': total, 'available': True}


@router.post('/translate/glossary/candidates/{candidate_id}/approve', dependencies=[Depends(require_translate)])
async def approve_glossary_candidate(candidate_id: int, body: GlossaryCandidateApprove, request: Request):
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    reviewer = identity['email'] or identity['user_id']

    async with pool.acquire() as conn:
        cand = await conn.fetchrow('SELECT * FROM glossary_candidates WHERE id = $1', candidate_id)
        if not cand:
            return JSONResponse({'error': 'Candidate not found'}, status_code=404)

        overrides = {'en': body.en, 'fr': body.fr, 'cs': body.cs, 'bg': body.bg, 'de': body.de, 'es': body.es,
                     'pt': body.pt, 'ar': body.ar}
        langs = {l: (v if v is not None else cand[l]) for l, v in overrides.items()}

        async with conn.transaction():
            # Claim first: a double-click or a second reviewer on the same
            # candidate used to insert the term twice.
            claimed = await conn.execute(
                '''
                UPDATE glossary_candidates
                SET status = 'approved', reviewed_by = $2, reviewed_at = NOW()
                WHERE id = $1 AND status = 'pending'
                ''',
                candidate_id, reviewer,
            )
            if claimed == 'UPDATE 0':
                return JSONResponse({'error': 'This candidate was already reviewed'}, status_code=409)
            term_id = await generate_term_id(conn)
            await conn.execute(
                '''
                INSERT INTO glossary_terms (term_id, en, fr, cs, bg, de, es, pt, ar, domain, notes,
                                            definition, definition_source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
                ''',
                term_id, langs['en'], langs['fr'], langs['cs'], langs['bg'], langs['de'], langs['es'],
                langs['pt'], langs['ar'], body.domain, body.notes, cand['definition'], cand['definition_source'],
            )
    return {'term_id': term_id}


@router.post('/translate/glossary/candidates/approve-all', dependencies=[Depends(require_translate)])
async def approve_all_glossary_candidates(request: Request):
    """Bulk-approve every pending candidate as-is (no per-row overrides) —
    the "Validate all" action once a reviewer has finished going through them."""
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    reviewer = identity['email'] or identity['user_id']

    async with pool.acquire() as conn:
        async with conn.transaction():
            pending = await conn.fetch("SELECT * FROM glossary_candidates WHERE status = 'pending' FOR UPDATE")
            # Single lock + MAX() read for the whole batch, then a local
            # counter — same allocation the shared generate_term_id() does
            # per-call, inlined here to avoid one advisory-lock round trip
            # per candidate.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext($1))", _glossary_lakebase.TERM_ID_LOCK_KEY)
            max_id = await conn.fetchval(
                r"SELECT COALESCE(MAX(SUBSTRING(term_id FROM 2)::int), 0) FROM glossary_terms WHERE term_id ~ '^T\d+$'"
            )
            next_id = (max_id or 0) + 1
            for cand in pending:
                term_id = f'T{next_id:04d}'
                next_id += 1
                await conn.execute(
                    '''
                    INSERT INTO glossary_terms (term_id, en, fr, cs, bg, de, es, pt, ar, domain, notes,
                                                definition, definition_source)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, '', '', $10, $11)
                    ''',
                    term_id, cand['en'], cand['fr'], cand['cs'], cand['bg'], cand['de'], cand['es'],
                    cand['pt'], cand['ar'], cand['definition'], cand['definition_source'],
                )
                await conn.execute(
                    '''
                    UPDATE glossary_candidates
                    SET status = 'approved', reviewed_by = $2, reviewed_at = NOW()
                    WHERE id = $1
                    ''',
                    cand['id'], reviewer,
                )
    return {'approved': len(pending)}


@router.post('/translate/glossary/candidates/{candidate_id}/reject', dependencies=[Depends(require_translate)])
async def reject_glossary_candidate(candidate_id: int, body: GlossaryCandidateReject, request: Request):
    pool = get_pool()
    if not pool:
        return JSONResponse({'error': 'Translation history not available'}, status_code=503)
    identity = await get_user_identity(request)
    reviewer = identity['email'] or identity['user_id']

    async with pool.acquire() as conn:
        result = await conn.execute(
            '''
            UPDATE glossary_candidates
            SET status = 'rejected', reviewed_by = $2, reviewed_at = NOW(), reject_reason = $3
            WHERE id = $1 AND status = 'pending'
            ''',
            candidate_id, reviewer, body.reason,
        )
    if result == 'UPDATE 0':
        return JSONResponse({'error': 'Candidate not found or already reviewed'}, status_code=404)
    return {'rejected': candidate_id}
