"""LLM verification pass for glossary term-pair candidates.

Catches extraction artifacts (a heading mistaken for a term, a sentence
fragment, two misaligned words) and normalizes stray casing before a candidate
is trusted. Shared by the offline corpus sync and the per-job glossary proposal.

Blocking (plain httpx calls with retry sleeps): async callers must run it via
asyncio.to_thread.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

LANG_NAMES = {'en': 'English', 'fr': 'French', 'cs': 'Czech', 'bg': 'Bulgarian', 'de': 'German', 'es': 'Spanish',
              'pt': 'Portuguese', 'ar': 'Arabic'}

_VERIFY_PROMPT = """You are a strict reviewer for a bilingual aerospace manufacturing glossary \
(LATECOERE). Each candidate below is a {lang_a_name}/{lang_b_name} term pair auto-extracted from \
a real professional translation (an official document and its official translation), so assume \
good faith by default - but the extraction sometimes produces artifacts: a document heading or \
title (often kept in ALL CAPS), a sentence fragment instead of a standalone term, or two words \
that got paired from misaligned sections and don't actually mean the same thing.

For each candidate, decide:
- keep: false only for headings/titles, sentence fragments, or genuinely mismatched pairs; \
true otherwise (default to true when unsure - do not reject a valid term just because it's rare).
- normalized casing for both sides: lowercase for ordinary common nouns/phrases (undo any ALL-CAPS \
or sentence-initial capital inherited from a document heading); keep the original casing only for \
genuine proper nouns, brand names, and acronyms (e.g. "NAS", "LATECOERE").

Return ONLY JSON: {{"verdicts": {{"<id>": {{"keep": true|false, "{lang_a}": "<normalized>", \
"{lang_b}": "<normalized>"}}, ...}}}}"""


def _post_with_backoff(url: str, headers: dict, payload: dict, retries: int = 4) -> httpx.Response:
    """POST to a rate-limited endpoint, retrying on HTTP 429.

    Waits for the Retry-After header when present, at least 20 seconds times the attempt number.

    :param url: request URL.
    :param headers: request headers.
    :param payload: JSON body.
    :param retries: number of retries after the first attempt.
    :return: the successful response; raises httpx.HTTPStatusError on other errors.
    """
    r = None
    for attempt in range(retries + 1):
        r = httpx.post(url, headers=headers, json=payload, timeout=120, verify=False)
        if r.status_code != 429 or attempt == retries:
            r.raise_for_status()
            return r
        try:
            wait = float(r.headers.get('retry-after', ''))
        except ValueError:
            wait = 0.0
        time.sleep(max(wait, 20 * (attempt + 1)))
    return r


def llm_verify(rows: list[dict], lang_a: str, lang_b: str, endpoint: str, host: str, token: str,
               batch: int = 25) -> list[dict]:
    """Filter and normalize term-pair candidates with an LLM.

    Fail-open: a batch that errors, or a row without a verdict, is kept unchanged.

    :param rows: candidate dicts keyed by language code.
    :param lang_a: first language code.
    :param lang_b: second language code.
    :param endpoint: serving endpoint name.
    :param host: Databricks workspace host.
    :param token: access token for the endpoint.
    :param batch: number of candidates per LLM call.
    :return: the kept candidates, with casing normalized where the LLM proposed it.
    """
    system = _VERIFY_PROMPT.format(
        lang_a_name=LANG_NAMES[lang_a], lang_b_name=LANG_NAMES[lang_b], lang_a=lang_a, lang_b=lang_b)
    kept: list[dict] = []
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        items = [{'id': str(j), lang_a: r[lang_a], lang_b: r[lang_b]} for j, r in enumerate(chunk)]
        verdicts: dict = {}
        try:
            resp = _post_with_backoff(
                f'{host}/serving-endpoints/{endpoint}/invocations',
                {'Authorization': f'Bearer {token}'},
                {'messages': [{'role': 'system', 'content': system},
                              {'role': 'user', 'content': json.dumps({'terms': items}, ensure_ascii=False)}],
                 'max_tokens': 3000, 'temperature': 0.0},
            )
            text = resp.json().get('choices', [{}])[0].get('message', {}).get('content', '')
            m = re.search(r'\{[\s\S]*\}', text)
            if m:
                verdicts = json.loads(m.group(0)).get('verdicts', {})
        except Exception as e:
            logger.warning('LLM verification batch failed (%s); batch kept unchanged (fail-open)', e)
        for j, row in enumerate(chunk):
            v = verdicts.get(str(j))
            if v is None:
                kept.append(row)
                continue
            if not v.get('keep', True):
                continue
            row = {**row}
            if v.get(lang_a):
                row[lang_a] = v[lang_a]
            if v.get(lang_b):
                row[lang_b] = v[lang_b]
            kept.append(row)
    return kept
