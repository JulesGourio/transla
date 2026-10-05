"""Page filter: spec parsing and the segment -> page mapping."""

from unittest.mock import patch

import pytest

from server.services.translation import pages as P


def test_parse_page_spec_accepts_ranges_and_lists():
    assert P.parse_page_spec('1-3,7,12-13') == {1, 2, 3, 7, 12, 13}
    assert P.parse_page_spec('  ') is None


@pytest.mark.parametrize('spec', ['5-3', '0', '0-2', '1-99999', '-3', 'a-b', '3-'])
def test_parse_page_spec_rejects_what_would_silently_change_the_filter(spec):
    with pytest.raises(ValueError):
        P.parse_page_spec(spec)


def _pdf(*pages):
    import pymupdf
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((72, 72 + 18 * i), line)
    data = doc.tobytes()
    doc.close()
    return data


def _seg(seg_id, text, part='word/document.xml'):
    return {'seg_id': seg_id, 'text': text, 'part': part}


def test_headers_and_footnotes_are_not_pinned_to_the_last_page():
    pdf = _pdf(['ACME HEADER', 'Alpha body text'], ['ACME HEADER', 'Beta body text'], ['ACME HEADER', 'Gamma body text'])
    segs = [_seg('a', 'Alpha body text'), _seg('c', 'Gamma body text'),
            _seg('h', 'ACME HEADER', 'word/header1.xml'), _seg('f', 'Gamma body text', 'word/footnotes.xml')]
    with patch.object(P, 'convert_docx_to_pdf', return_value=pdf):
        pages = P.assign_pages(segs, b'')
    assert pages == {'a': 1, 'c': 3}


def test_a_segment_wrapped_over_two_lines_of_the_pdf_is_still_placed():
    pdf = _pdf(['first page'], ['Torque the nut', 'to the value'])
    with patch.object(P, 'convert_docx_to_pdf', return_value=pdf):
        pages = P.assign_pages([_seg('t', 'Torque the nut to the value')], b'')
    assert pages == {'t': 2}
