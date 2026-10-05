"""_translate_unique_strings: retries, persistence callbacks, failure reporting."""

import asyncio
from unittest.mock import patch

from server.routers import translate as T


def _run_stage(strings, fake_batch, on_resolved=None):
    async def go():
        with patch.object(T, '_translate_batch', side_effect=fake_batch), \
                patch.object(T, '_update_job', return_value=None):
            return await T._translate_unique_strings(
                'h', 't', 'ep', strings, 'bg', 'fr', [], 1, on_resolved=on_resolved)
    return asyncio.run(go())


def test_a_failed_write_is_retried_instead_of_counted_as_persisted():
    persisted = {}
    calls = {'n': 0}

    async def batch(host, token, endpoint, strings, *a, **k):
        return {s: f'fr:{s}' for s in strings}

    async def on_resolved(items):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('connection reset')
        persisted.update(items)

    translations, failed = _run_stage(['un', 'deux'], batch, on_resolved)
    assert persisted == {'un': 'fr:un', 'deux': 'fr:deux'}
    assert failed == []


def test_string_that_can_never_be_persisted_is_reported_failed_not_translated():
    async def batch(host, token, endpoint, strings, *a, **k):
        return {s: f'fr:{s}' for s in strings}

    async def on_resolved(items):
        raise RuntimeError('db down')

    translations, failed = _run_stage(['un'], batch, on_resolved)
    assert failed == ['un']
    assert 'un' not in translations


def test_second_attempt_only_resends_the_strings_still_missing():
    sent = []

    async def batch(host, token, endpoint, strings, *a, **k):
        sent.append(list(strings))
        return {s: f'fr:{s}' for s in strings if len(sent) > 1 or s != 'deux'}

    translations, failed = _run_stage(['un', 'deux'], batch)
    assert sent == [['un', 'deux'], ['deux']]
    assert translations == {'un': 'fr:un', 'deux': 'fr:deux'}
    assert failed == []
