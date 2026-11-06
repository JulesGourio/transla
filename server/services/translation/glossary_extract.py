"""Job-driven glossary candidate extraction.

A validated job proposes term candidates by LLM extraction over its own
source->target segment pairs, and a human confirms them through the
glossary_candidates pending-review queue. Candidates land at status='pending'
and are never auto-published.
"""
from __future__ import annotations

import unicodedata
from collections import Counter
from typing import Any

from ..llm import call_llm_json

LANGS = ('en', 'fr', 'cs', 'bg', 'de', 'es', 'pt', 'ar')


def normalize_term(term: str) -> str:
    """Normalize a glossary term for comparison: lowercase, trimmed, accents removed.

    :param term: raw term.
    :return: normalized term.
    """
    t = unicodedata.normalize('NFKD', term.lower().strip())
    return ''.join(c for c in t if not unicodedata.combining(c))

_LANG_NAMES = {'en': 'English', 'fr': 'French', 'cs': 'Czech', 'bg': 'Bulgarian', 'de': 'German', 'es': 'Spanish',
               'pt': 'Portuguese', 'ar': 'Arabic'}

_EXTRACT_PROMPT = """You are a bilingual aerospace terminology extractor.
Given numbered pairs of a {source_name} sentence and its confirmed {target_name} \
translation (from a real, human-reviewed translation job), extract the technical \
term correspondences (tools, operations, components, materials, defects) that \
appear in BOTH sentences of a pair. Only terms clearly present in both texts; \
no part numbers, no standards references, no full sentences — noun phrases of \
1-4 words.
Return ONLY JSON: {{"terms": [{{"{source_code}": "<term>", "{target_code}": "<term>"}}, ...]}}"""


async def extract_job_candidates(
    pairs: list[tuple[str, str]],
    source_lang: str,
    target_lang: str,
    host: str,
    token: str,
    endpoint: str,
    max_pairs: int = 40,
) -> list[dict[str, str]]:
    """Propose glossary term pairs from the translated segments of one job, using an LLM.

    Pairs are deduplicated and ranked by repeat count first, so only the most frequent ones fit the prompt; a phrase recurring within the job is the strongest sign of a real term.

    :param pairs: (source_text, translated_text) of segments translated as a whole.
    :param source_lang: source language code.
    :param target_lang: target language code.
    :param host: Databricks workspace host.
    :param token: access token for the LLM endpoint.
    :param endpoint: serving endpoint name.
    :param max_pairs: maximum number of distinct pairs sent to the LLM.
    :return: candidate dicts {source_lang: term, target_lang: term}; empty when there are no pairs.
    """
    counts = Counter(pairs)
    top = [p for p, _n in counts.most_common(max_pairs)]
    if not top:
        return []

    system = _EXTRACT_PROMPT.format(
        source_name=_LANG_NAMES.get(source_lang, source_lang.upper()),
        target_name=_LANG_NAMES.get(target_lang, target_lang.upper()),
        source_code=source_lang, target_code=target_lang,
    )
    user = '\n'.join(f'{i}. {src} -> {tgt}' for i, (src, tgt) in enumerate(top))
    messages = [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user},
    ]
    result, _usage = await call_llm_json(host, token, endpoint, messages)
    terms: list[Any] = result.get('terms', []) if isinstance(result, dict) else []
    return [
        {source_lang: t[source_lang].strip(), target_lang: t[target_lang].strip()}
        for t in terms
        if isinstance(t, dict) and t.get(source_lang) and t.get(target_lang)
    ]
