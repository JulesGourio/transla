"""LLM translation of unique strings: prompts, glossary and memory context, batching, retries and concurrency caps."""

import asyncio
import contextlib
import json
import logging
import os
from typing import Awaitable, Callable, Dict, List, Optional

from ..llm import call_llm_json
from .jobs_db import record_llm_usage, update_job

logger = logging.getLogger(__name__)


LANG_NAMES = {'en': 'English', 'fr': 'French', 'es': 'Spanish', 'de': 'German', 'cs': 'Czech', 'bg': 'Bulgarian',
               'pt': 'Portuguese', 'ar': 'Arabic'}


TRANSLATE_BATCH_SIZE = 40
# call_llm_json caps the answer at 8192 tokens: a batch must stay small enough for its translation to fit.
TRANSLATE_BATCH_MAX_CHARS = int(os.getenv('TRANSLATE_BATCH_MAX_CHARS', '6000'))
_translate_semaphore = asyncio.Semaphore(int(os.getenv('TRANSLATE_MAX_CONCURRENT', '3')))
# A single job's batches must not monopolize the whole _translate_semaphore
# pool while another job's batches queue behind it — this caps any one job's
# share, acquired in addition to (nested inside) the global semaphore.
_TRANSLATE_MAX_CONCURRENT_PER_JOB = int(os.getenv('TRANSLATE_MAX_CONCURRENT_PER_JOB', '2'))


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

# The residual re-pass states why the strings are back: repeating the plain prompt mostly returns the same
# untranslated output.
_RETRY_PROMPT_SUFFIX = """IMPORTANT: a previous pass returned these strings still in {source_name}. \
Every {source_name} word must now be translated into {target_name}; only part numbers, codes, \
standards references and proper nouns may stay unchanged."""


GLOSSARY_MAX_LINES = 300


def build_glossary_context(
    glossary_rows: List[Dict[str, str]], source_lang: str, target_lang: str,
    batch_strings: Optional[List[str]] = None,
) -> str:
    """Format glossary entries as 'source -> target' lines for the system prompt.

    :param glossary_rows: glossary terms keyed by language code.
    :param source_lang: language code of the source side.
    :param target_lang: language code of the target side.
    :param batch_strings: when given, only terms whose source form occurs in these strings (case-insensitive) are kept.
    :return: newline-joined lines, at most GLOSSARY_MAX_LINES; empty when nothing matches.
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
        if len(lines) >= GLOSSARY_MAX_LINES:
            break
    return '\n'.join(lines)


MEMORY_MAX_LINES = 100


def build_memory_context(resolved_memory: Dict[str, str], batch_strings: List[str]) -> str:
    """Format translations already resolved earlier in the job that match the current batch.

    :param resolved_memory: source string -> translation resolved by earlier batches of the same job.
    :param batch_strings: strings of the current batch.
    :return: newline-joined 'source -> target' lines whose source occurs in the batch, at most MEMORY_MAX_LINES.
    """
    blob = '\n'.join(batch_strings).lower()
    lines = []
    for src, tgt in resolved_memory.items():
        if src.lower() not in blob:
            continue
        lines.append(f'{src} -> {tgt}')
        if len(lines) >= MEMORY_MAX_LINES:
            break
    return '\n'.join(lines)


async def translate_batch(
    host: str, token: str, endpoint: str, strings: List[str],
    source_lang: str, target_lang: str, glossary_rows: List[Dict[str, str]], job_id: int,
    job_semaphore: Optional[asyncio.Semaphore] = None,
    resolved_memory: Optional[Dict[str, str]] = None,
    retry: bool = False,
) -> Dict[str, str]:
    """Translate one batch of strings with a single LLM call.

    :param host: workspace URL.
    :param token: bearer token.
    :param endpoint: serving endpoint name.
    :param strings: strings to translate; each is sent under its numeric index as id.
    :param source_lang: source language code.
    :param target_lang: target language code.
    :param glossary_rows: glossary terms used to build the prompt context.
    :param job_id: translation_jobs.id the usage is recorded against.
    :param job_semaphore: optional per-job concurrency cap, acquired before the global one.
    :param resolved_memory: optional source -> translation pairs from earlier batches, added to the prompt.
    :param retry: True for the residual re-pass, which adds a note that the strings came back untranslated.
    :return: source string -> translation; strings the model left missing or empty are absent.
    """
    source_name = LANG_NAMES.get(source_lang, source_lang.upper())
    target_name = LANG_NAMES.get(target_lang, target_lang.upper())
    system_prompt = _TRANSLATE_SYSTEM_PROMPT.format(source_name=source_name, target_name=target_name)
    if retry:
        system_prompt += '\n\n' + _RETRY_PROMPT_SUFFIX.format(source_name=source_name, target_name=target_name)
    glossary_context = build_glossary_context(glossary_rows, source_lang, target_lang, batch_strings=strings)
    if glossary_context:
        system_prompt += f'\n\nGlossary ({source_lang}->{target_lang}):\n{glossary_context}'
    memory_context = build_memory_context(resolved_memory, strings) if resolved_memory else ''
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
    await record_llm_usage(job_id, endpoint, len(strings), usage)
    raw = result.get('translations', {}) if isinstance(result, dict) else {}
    out: Dict[str, str] = {}
    for i, s in enumerate(strings):
        value = raw.get(str(i))
        if isinstance(value, str) and value.strip():
            out[s] = value
    return out


def make_batches(strings: List[str]) -> List[List[str]]:
    """Group consecutive strings into batches bounded by count and total characters.

    :param strings: strings in document order.
    :return: batches of at most TRANSLATE_BATCH_SIZE strings and TRANSLATE_BATCH_MAX_CHARS characters; a longer string travels alone.
    """
    batches: List[List[str]] = []
    current: List[str] = []
    chars = 0
    for s in strings:
        if current and (len(current) >= TRANSLATE_BATCH_SIZE or chars + len(s) > TRANSLATE_BATCH_MAX_CHARS):
            batches.append(current)
            current, chars = [], 0
        current.append(s)
        chars += len(s)
    if current:
        batches.append(current)
    return batches


async def translate_unique_strings(
    host: str, token: str, endpoint: str, unique_strings: List[str],
    source_lang: str, target_lang: str, glossary_rows: List[Dict[str, str]], job_id: int,
    on_resolved: Optional[Callable[[Dict[str, str]], Awaitable[None]]] = None,
    progress_offset: int = 0, progress_total: Optional[int] = None,
    retry: bool = False,
) -> tuple[Dict[str, str], List[str]]:
    """Batch-translate unique strings, retrying failed batches whole and then string by string.

    :param host: workspace URL.
    :param token: bearer token.
    :param endpoint: serving endpoint name.
    :param unique_strings: distinct strings to translate, in document order.
    :param source_lang: source language code.
    :param target_lang: target language code.
    :param glossary_rows: glossary terms used to build the prompt context.
    :param job_id: translation_jobs.id for usage and progress.
    :param on_resolved: awaited with each batch of newly resolved source -> translation pairs once they are available.
    :param progress_offset: number of strings already resolved before this call, added to the progress counter.
    :param progress_total: total shown in the progress counter; defaults to len(unique_strings).
    :param retry: True inside the rebuild stage, where the job status is left untouched.
    :return: (translations, failed_strings); failed strings are those still unresolved after every retry.
    """
    translations: Dict[str, str] = {}
    failed: List[str] = []
    # Populated as batches complete; later-starting batches (queued behind
    # _translate_semaphore) see terms already resolved by earlier ones,
    # improving cross-batch terminology consistency at no extra LLM cost.
    resolved_memory: Dict[str, str] = {}
    # This job's own share of the global _translate_semaphore pool (see
    # translate_batch) — keeps one large job from starving another job's
    # batches queued behind the same global cap.
    job_semaphore = asyncio.Semaphore(min(_TRANSLATE_MAX_CONCURRENT_PER_JOB, int(os.getenv('TRANSLATE_MAX_CONCURRENT', '3'))))
    batches = make_batches(unique_strings)

    async def _run_batch(batch: List[str]) -> None:
        """Translate one batch, retrying it whole and then each unresolved string on its own.

        :param batch: strings of the batch.
        """
        applied: set[str] = set()
        todo = batch
        for attempt in range(2):
            try:
                result = await translate_batch(
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
                    result = await translate_batch(
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
        """Run one batch to completion.

        :param batch: strings of the batch.
        :return: number of strings in the batch.
        """
        await _run_batch(batch)
        return len(batch)

    tasks = [asyncio.create_task(_run_and_count(b)) for b in batches]
    for finished in asyncio.as_completed(tasks):
        done_strings += await finished
        translated_so_far = progress_offset + min(done_strings, len(unique_strings))
        if retry:
            continue  # runs inside the rebuild stage: don't flip the job status back to 'translating'
        await update_job(
            job_id, status='translating',
            stage_progress={'stage': 'translating', 'done': min(translated_so_far, total), 'total': total},
        )

    return translations, failed
