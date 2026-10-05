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


def test_batches_respect_the_character_budget():
    long = 'x' * 4000
    batches = T._make_batches([long, long, 'a', 'b'])
    assert batches == [[long], [long, 'a', 'b']]


def test_a_string_longer_than_the_budget_travels_alone():
    huge = 'y' * 20000
    assert T._make_batches(['a', huge, 'b']) == [['a'], [huge], ['b']]


def test_batches_keep_the_item_cap_and_the_order():
    strings = [str(i) for i in range(95)]
    batches = T._make_batches(strings)
    assert [len(b) for b in batches] == [40, 40, 15]
    assert [s for b in batches for s in b] == strings


def test_progress_counts_translated_strings_across_uneven_batches():
    seen = []

    async def update(job_id, **kw):
        seen.append(kw['stage_progress']['done'])

    async def batch(host, token, endpoint, strings, *a, **k):
        return {s: s.upper() for s in strings}

    async def go():
        with patch.object(T, '_translate_batch', side_effect=batch), patch.object(T, '_update_job', side_effect=update):
            return await T._translate_unique_strings('h', 't', 'ep', ['a' * 5000, 'b' * 5000, 'c'], 'bg', 'fr', [], 1)

    asyncio.run(go())
    assert max(seen) == 3
