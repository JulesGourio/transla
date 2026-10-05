"""Minimal LLM endpoint calling — Databricks Model Serving, JSON output.

Standalone implementation of just what the translate router needs
(call_llm_json, cost_eur, _fix_mojibake) — no SSE relay or MLflow tracing.
"""

import json
import logging
import os
import asyncio
import re
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = float(os.getenv('TRANSLATE_ENDPOINT_TIMEOUT_S', '300'))
DEFAULT_CONNECT_TIMEOUT_S = float(os.getenv('TRANSLATE_ENDPOINT_CONNECT_TIMEOUT_S', '30'))

# Waits before each retry of a transient failure (rate limit, gateway error,
# timeout, dropped connection). Without them the caller's immediate retries all
# hit the same 429 and a whole document came back "translation failed".
_RETRY_DELAYS_S = (2.0, 6.0, 15.0)
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}

# DBU/1M-token rates x contracted EUR/DBU (confirmed 2026-07-29 from the
# workspace Serving Endpoints console) — kept in sync manually.
_PRICING_USD: Dict[str, Dict[str, float]] = {
    'databricks-gpt-5-4-mini': {'input': 1.817, 'output': 10.901},
    'databricks-gpt-5-mini': {'input': 0.545, 'output': 2.422},
    'databricks-gemini-3-1-flash-lite': {'input': 0.545, 'output': 3.270},
    'databricks-gpt-5-6-luna': {'input': 1.211, 'output': 10.901},
}
_DEFAULT_PRICING = {'input': 3.0, 'output': 15.0}
_EUR_PER_USD = float(os.getenv('EUR_PER_USD', '0.92'))


def _fix_mojibake(text: str) -> str:
    if not text:
        return text
    try:
        return text.encode('latin-1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    def _fix_segment(match):
        seg = match.group(0)
        try:
            return seg.encode('latin-1').decode('utf-8')
        except (UnicodeDecodeError, UnicodeEncodeError):
            return seg

    return re.sub(r'[-ÿ]+', _fix_segment, text)


def _cost_eur(endpoint_name: str, input_tokens: int, output_tokens: int) -> float:
    p = _PRICING_USD.get(endpoint_name, _DEFAULT_PRICING)
    return round((input_tokens * p['input'] + output_tokens * p['output']) / 1_000_000 * _EUR_PER_USD, 6)


cost_eur = _cost_eur


async def call_llm_json(
    host: str,
    token: str,
    endpoint_name: str,
    messages: List[Dict[str, Any]],
    max_tokens: int = 8192,
    temperature: float = 0.0,
) -> tuple[Dict[str, Any], Dict[str, int]]:
    """Single non-streaming chat-completion call, parsed as one JSON object.

    Retries once with a repair instruction if the response isn't valid JSON.
    Returns (parsed_json, usage) where usage is {'input_tokens', 'output_tokens'}.
    """
    url = f'{host}/serving-endpoints/{endpoint_name}/invocations'
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}
    timeout = httpx.Timeout(DEFAULT_TIMEOUT_S, connect=DEFAULT_CONNECT_TIMEOUT_S)
    # Some models (confirmed: databricks-gpt-5-6-luna) reject an explicit
    # `temperature` entirely — 400 on any value other than their fixed
    # default. Detected once per call_llm_json invocation and remembered so
    # the JSON-repair retry below doesn't pay the same failed round-trip twice.
    skip_temperature = False

    async def _post(payload: Dict[str, Any]) -> httpx.Response:
        for attempt in range(len(_RETRY_DELAYS_S) + 1):
            wait = _RETRY_DELAYS_S[attempt] if attempt < len(_RETRY_DELAYS_S) else None
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if wait is None:
                    raise
                logger.warning('call_llm_json: %s %s — retrying in %.0fs', endpoint_name, type(e).__name__, wait)
                await asyncio.sleep(wait)
                continue
            if resp.status_code not in _TRANSIENT_STATUS or wait is None:
                return resp
            try:
                wait = max(wait, min(float(resp.headers.get('retry-after', 0)), 60.0))
            except ValueError:
                pass
            logger.warning('call_llm_json: %s returned %s — retrying in %.0fs', endpoint_name, resp.status_code, wait)
            await asyncio.sleep(wait)
        raise AssertionError('unreachable')

    def _rejects_temperature(resp: httpx.Response) -> bool:
        # The error is nested JSON-as-a-string inside the outer error body
        # (escaped quotes), so match loosely rather than on an exact
        # `"param": "temperature"` substring.
        if resp.status_code != 400:
            return False
        text = resp.text.lower()
        return 'temperature' in text and ('unsupported_value' in text or 'does not support' in text)

    async def _call(msgs: List[Dict[str, Any]]) -> tuple[str, Dict[str, int]]:
        nonlocal skip_temperature
        payload = {'messages': msgs, 'max_tokens': max_tokens, 'stream': False}
        if not skip_temperature:
            payload['temperature'] = temperature
        resp = await _post(payload)
        if not skip_temperature and _rejects_temperature(resp):
            logger.warning('call_llm_json: %s rejected temperature=%s — retrying without it',
                            endpoint_name, temperature)
            skip_temperature = True
            del payload['temperature']
            resp = await _post(payload)
        if resp.status_code != 200:
            raise RuntimeError(f'{endpoint_name} returned {resp.status_code}: {resp.text[:500]}')
        body = resp.json()
        choices = body.get('choices') or []
        if not choices:
            raise RuntimeError(f'{endpoint_name} returned no choices')
        content = choices[0].get('message', {}).get('content', '')
        truncated = choices[0].get('finish_reason') == 'length'
        if isinstance(content, list):
            content = ''.join(item.get('text', '') if isinstance(item, dict) else str(item) for item in content)
        u = body.get('usage') or {}
        usage = {'input_tokens': u.get('prompt_tokens') or 0, 'output_tokens': u.get('completion_tokens') or 0}
        content = _fix_mojibake(content)
        if truncated:
            # A repair prompt cannot restore text the model never wrote.
            try:
                _extract_json(content)
            except json.JSONDecodeError:
                raise RuntimeError(f'{endpoint_name} answer cut off at max_tokens={max_tokens}')
        return content, usage

    def _extract_json(text: str) -> Dict[str, Any]:
        text = text.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        return json.loads(text)

    raw, usage = await _call(messages)
    try:
        return _extract_json(raw), usage
    except json.JSONDecodeError as e:
        logger.warning('call_llm_json: response from %s was not valid JSON (%s) — retrying with repair prompt',
                        endpoint_name, e)
        repair_messages = messages + [
            {'role': 'assistant', 'content': raw},
            {'role': 'user', 'content': 'Your previous response was not valid JSON. '
                                         'Reply with ONLY the corrected JSON object — no prose, no markdown fences.'},
        ]
        raw2, usage2 = await _call(repair_messages)
        usage['input_tokens'] += usage2['input_tokens']
        usage['output_tokens'] += usage2['output_tokens']
        return _extract_json(raw2), usage  # a second failure propagates to the caller
