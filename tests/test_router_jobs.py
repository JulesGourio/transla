"""Router-level behaviour that needs a database, run against tests/fakedb.py."""

import asyncio
from unittest.mock import AsyncMock, patch

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


# --- rebuild stage, run end to end on a tiny document against the fake DB ---

import io
import json
import zipfile

from server.services.processors.translation import extract_docx_segments
from tests.docx_factory import build_docx, para


def _seed_job(pool, docx_bytes, rows):
    """rows: seg index -> dict of translation_segments column overrides."""
    pool.add_job(id=1, user_id='u', status='translated', source_lang='bg', target_lang='fr',
                 input_volume_path='/in/input.docx', needs_translation_count=len(rows))
    for i, seg in enumerate(extract_docx_segments(docx_bytes)):
        fields = dict(job_id=1, seg_id=seg['seg_id'], part=seg['part'], location_type=seg['location_type'],
                      xml_choice_path=json.dumps(seg['xml_choice_path']), xml_fallback_path='null',
                      source_text=seg['text'], detected_lang='bg', pattern_type='mono', dnt_tokens='[]')
        fields.update(rows[i])
        pool.add_segment(**fields)


def _run_rebuild(pool, docx_bytes):
    uploaded = {}
    with patch.object(T, 'get_pool', return_value=pool), \
            patch.object(T, '_download_from_volume', AsyncMock(return_value=docx_bytes)), \
            patch.object(T._storage, 'upload', side_effect=lambda path, data: uploaded.update({path: data})), \
            patch.object(T._storage, 'job_root', return_value='/out'), \
            patch.object(T, '_generate_preview_pdfs', AsyncMock(return_value=None)):
        _run(T._run_rebuild_stage(1, 'bg'))
    return uploaded


def _doc_texts(data):
    xml = zipfile.ZipFile(io.BytesIO(data)).read('word/document.xml').decode()
    return [t.split('<')[0] for t in xml.split('<w:t>')[1:]] + [t.split('<')[0] for t in xml.split('<w:t xml:space="preserve">')[1:]]


def test_segment_without_translation_keeps_its_source_and_is_flagged_instead_of_crashing_the_rebuild():
    docx = build_docx(para('Premier') + para('Second') + para('Troisieme'))
    pool = FakePool()
    _seed_job(pool, docx, [
        dict(translated_text='First', keep_as_is=0),
        dict(translated_text=None, keep_as_is=1),
        dict(translated_text=None, keep_as_is=0),  # planned, write lost
    ])
    uploaded = _run_rebuild(pool, docx)
    job = pool.job()
    assert job['error_msg'] is None
    assert job['status'] == 'done_with_warnings'
    assert sorted(_doc_texts(uploaded['/out/1/output.docx'])) == ['First', 'Second', 'Troisieme']
    lost = pool.segment('word/document.xml#body_direct.bp2#p0')
    assert lost['conflict_flag'] == 1 and lost['conflict_detail'].startswith('Translation missing')


def test_translating_a_segment_that_failed_before_clears_the_failure_flag():
    pool = FakePool()
    pool.add_job(id=1)
    pool.add_segment(job_id=1, seg_id='s1', source_text='Здравей', pattern_type='mono', conflict_flag=1,
                     conflict_detail='Translation failed after all retries — use the translate button',
                     dnt_tokens='[]', keep_as_is=1)
    pool.add_segment(job_id=1, seg_id='s2', source_text='Здравей', pattern_type='mono', conflict_flag=1,
                     conflict_detail='Same source text translated differently elsewhere in this document',
                     dnt_tokens='[]')
    plan = {'compose': lambda tr: tr, 'kept_text': '', 'inline_json': None}

    async def go():
        async with pool.acquire() as conn:
            for sid in ('s1', 's2'):
                row = dict(await conn.fetchrow('SELECT * FROM translation_segments WHERE seg_id = $1', sid))
                await T._apply_resolved_segment(conn, row, plan, 'Bonjour', {}, 'bg')

    _run(go())
    failed_before = pool.segment('s1')
    assert (failed_before['translated_text'], failed_before['conflict_flag'], failed_before['conflict_detail']) \
        == ('Bonjour', 0, None)
    # an unrelated flag survives
    assert pool.segment('s2')['conflict_flag'] == 1


# --- previews: never stale, never a failed job's file ---

from types import SimpleNamespace


def _request(**headers):
    return SimpleNamespace(headers=headers)


def test_preview_is_refused_for_a_job_whose_validation_failed():
    pool = FakePool()
    pool.add_job(id=1, user_id='u', status='failed', input_volume_path='/in.docx', output_volume_path='/out.docx')
    with patch.object(T, 'get_pool', return_value=pool), \
            patch.object(T, 'get_user_identity', AsyncMock(return_value={'user_id': 'u'})), \
            patch.object(T, '_download_from_volume', AsyncMock(return_value=b'broken')):
        for fmt in ('docx', 'pdf'):
            resp = _run(T.get_translation_preview(1, _request(), format=fmt))
            assert resp.status_code == 409
        assert _run(T.get_translation_preview_pdf(1, _request(), side='after')).status_code == 409


def test_preview_is_served_once_the_job_is_done():
    pool = FakePool()
    pool.add_job(id=1, user_id='u', status='done', input_volume_path='/in.docx', output_volume_path='/out.docx')
    with patch.object(T, 'get_pool', return_value=pool), \
            patch.object(T, 'get_user_identity', AsyncMock(return_value={'user_id': 'u'})), \
            patch.object(T, '_download_from_volume', AsyncMock(return_value=b'docx-bytes')):
        resp = _run(T.get_translation_preview(1, _request(), format='docx'))
    assert set(resp) == {'before_docx_base64', 'after_docx_base64'}


def test_shared_view_does_not_ship_the_file_of_a_failed_job():
    pool = FakePool()
    pool.add_job(id=1, user_id='u', status='failed', share_token='tok', output_volume_path='/out.docx')
    with patch.object(T, 'get_pool', return_value=pool), \
            patch.object(T, '_download_from_volume', AsyncMock(return_value=b'broken')):
        shared = _run(T.get_shared_translation_job('tok'))
    assert shared['output_docx_base64'] is None


def test_pdf_responses_are_revalidated_not_cached_for_ten_minutes():
    first = T._revalidating_response(b'%PDF-1', 'application/pdf', _request())
    assert first.headers['cache-control'] == 'private, no-cache'
    again = T._revalidating_response(b'%PDF-1', 'application/pdf', _request(**{'if-none-match': first.headers['etag']}))
    assert again.status_code == 304
    rebuilt = T._revalidating_response(b'%PDF-2', 'application/pdf', _request(**{'if-none-match': first.headers['etag']}))
    assert rebuilt.status_code == 200 and rebuilt.body == b'%PDF-2'


def test_rebuild_deletes_the_previous_builds_preview_pdfs_first():
    docx = build_docx(para('Premier'))
    pool = FakePool()
    _seed_job(pool, docx, [dict(translated_text='First', keep_as_is=0)])
    deleted = []
    with patch.object(T._storage, 'delete', side_effect=deleted.append):
        _run_rebuild(pool, docx)
    assert deleted == ['/out/1/preview_original.pdf', '/out/1/preview_translated.pdf']
