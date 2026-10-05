"""Router-level behaviour that needs a database, run against tests/fakedb.py."""

import asyncio
from unittest.mock import patch

from server.routers import translate as T
from tests.fakedb import FakePool


def _run(coro):
    return asyncio.run(coro)


def test_background_upload_does_not_wipe_the_error_of_a_failed_job():
    pool = FakePool()
    pool.add_job(id=1, status='failed', error_type='BadZipFile', error_msg='File is not a zip file')
    with patch.object(T, 'get_pool', return_value=pool), \
            patch.object(T._storage, 'upload', return_value=None):
        _run(T._upload_input_to_volume(1, b'not a docx', 'a.docx'))
    job = pool.job()
    assert job['input_volume_path'].endswith('input_a.docx')
    assert (job['status'], job['error_type'], job['error_msg']) == ('failed', 'BadZipFile', 'File is not a zip file')


def test_status_update_still_sets_and_clears_the_error():
    pool = FakePool()
    pool.add_job(id=1, status='uploaded')
    with patch.object(T, 'get_pool', return_value=pool):
        _run(T._update_job(1, status='failed', error_type='X', error_msg='boom'))
        assert pool.job()['error_msg'] == 'boom'
        _run(T._update_job(1, status='extracting'))
    assert pool.job()['error_msg'] is None
