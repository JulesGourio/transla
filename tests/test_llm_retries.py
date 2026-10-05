"""call_llm_json: transient endpoint failures are retried with a wait, not hammered."""

import asyncio

import httpx
import pytest

from server.services import llm


def _client_for(handler):
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    return lambda **kw: real(transport=transport, **kw)


def _ok(content='{"translations": {"0": "bonjour"}}', finish='stop'):
    return httpx.Response(200, json={'choices': [{'message': {'content': content}, 'finish_reason': finish}],
                                     'usage': {'prompt_tokens': 1, 'completion_tokens': 1}})


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(llm, '_RETRY_DELAYS_S', (0.0, 0.0, 0.0))


def _call(handler, monkeypatch):
    monkeypatch.setattr(httpx, 'AsyncClient', _client_for(handler))
    return asyncio.run(llm.call_llm_json('https://h', 't', 'ep', [{'role': 'user', 'content': 'x'}]))


def test_rate_limit_is_retried_until_it_clears(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, text='slow down') if len(calls) < 3 else _ok()

    result, _ = _call(handler, monkeypatch)
    assert result == {'translations': {'0': 'bonjour'}}
    assert len(calls) == 3


def test_timeout_is_retried(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout('slow', request=request)
        return _ok()

    _call(handler, monkeypatch)
    assert len(calls) == 2


def test_persistent_gateway_error_gives_up_with_its_status(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text='unavailable')

    with pytest.raises(RuntimeError, match='503'):
        _call(handler, monkeypatch)
    assert len(calls) == 4


def test_client_error_is_not_retried(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, text='bad request')

    with pytest.raises(RuntimeError, match='400'):
        _call(handler, monkeypatch)
    assert len(calls) == 1


def test_answer_cut_off_at_max_tokens_is_not_sent_for_repair(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return _ok('{"translations": {"0": "Le début de', finish='length')

    with pytest.raises(RuntimeError, match='cut off'):
        _call(handler, monkeypatch)
    assert len(calls) == 1
