"""extract.py: every piece of text must come out exactly once, with its own id."""

import io
import zipfile

from lxml import etree

from server.services.processors.translation import build_rebuild_inputs, extract_docx_segments, rebuild_docx_bytes
from server.services.translation import audit as _audit
from tests.docx_factory import W, build_docx, para, table, textbox_run


def _ids(segments):
    return [s['seg_id'] for s in segments]


def test_text_boxes_in_different_table_cells_get_distinct_ids():
    cell = lambda t: f'<w:p>{textbox_run(para(t))}</w:p>'
    segs = extract_docx_segments(build_docx(table(cell('Alpha one'), cell('Beta two'))))
    assert sorted(s['text'] for s in segs) == ['Alpha one', 'Beta two']
    assert len(set(_ids(segs))) == 2


def test_two_tables_in_one_content_control_get_distinct_ids():
    body = f'<w:sdt><w:sdtContent>{table(para("T one"))}{table(para("T two"))}</w:sdtContent></w:sdt>'
    segs = extract_docx_segments(build_docx(body))
    assert [s['text'] for s in segs] == ['T one', 'T two']
    assert len(set(_ids(segs))) == 2


def test_nested_content_control_does_not_reuse_the_outer_paragraph_id():
    body = (f'<w:sdt><w:sdtContent>{para("outer")}'
            f'<w:sdt><w:sdtContent>{para("inner")}</w:sdtContent></w:sdt></w:sdtContent></w:sdt>')
    segs = extract_docx_segments(build_docx(body))
    assert [s['text'] for s in segs] == ['outer', 'inner']
    assert len(set(_ids(segs))) == 2


def test_ids_that_never_collided_are_unchanged():
    segs = extract_docx_segments(build_docx(para('One') + para('Two')))
    assert _ids(segs) == ['word/document.xml#body_direct.bp0#p0', 'word/document.xml#body_direct.bp1#p0']


def test_content_control_inside_a_table_cell_is_extracted():
    body = table(f'<w:sdt><w:sdtContent>{para("Hidden in a control")}</w:sdtContent></w:sdt>')
    assert [s['text'] for s in extract_docx_segments(build_docx(body))] == ['Hidden in a control']


def test_vml_text_box_without_alternate_content_is_extracted():
    body = ('<w:p><w:r><w:pict><v:shape><v:textbox><w:txbxContent>'
            f'{para("Legacy box")}</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>')
    segs = extract_docx_segments(build_docx(body))
    assert [s['text'] for s in segs] == ['Legacy box']
    assert segs[0]['location_type'] == 'txbx'


def test_text_box_choice_and_fallback_copies_are_still_one_segment():
    segs = extract_docx_segments(build_docx(f'<w:p>{textbox_run(para("Boxed"))}</w:p>'))
    assert [s['text'] for s in segs] == ['Boxed']
    assert segs[0]['xml_fallback_path']


def test_table_inside_a_text_box_is_extracted_and_translated_in_both_copies():
    src = build_docx(f'<w:p>{textbox_run(table(para("Cell in box")))}</w:p>')
    segs = extract_docx_segments(src)
    assert [s['text'] for s in segs] == ['Cell in box']
    assert segs[0]['xml_fallback_path']
    rows = [{**s, 'original_text': s['text'], 'translated_text': 'Cellule', 'keep_as_is': False,
             'pattern_type': 'mono', 'inline_split': None} for s in segs]
    by_part, meta = build_rebuild_inputs(rows)
    out = rebuild_docx_bytes(src, by_part, meta)
    root = etree.fromstring(zipfile.ZipFile(io.BytesIO(out)).read('word/document.xml'))
    assert [t.text for t in root.iter(W + 't')] == ['Cellule', 'Cellule']


def test_text_boxes_in_different_cells_are_never_paired_with_each_other():
    cell = lambda t: f'<w:p>{textbox_run(para(t))}</w:p>'
    segs = extract_docx_segments(build_docx(table(cell('Alpha one'), cell('Beta two'))))
    audited = [{'seg_id': s['seg_id'], 'text': s['text'], 'pair_id': None, 'pattern_type': 'mono',
                'detected_lang': lang, '_location': s['location_type'], '_part': s['part'],
                '_txbx_path': s['txbx_path'], '_para_idx': s['para_idx_in_container']}
               for s, lang in zip(segs, ('bg', 'en'))]
    _audit.pair_txbx_siblings(audited, _audit.Pairer())
    assert all(a['pair_id'] is None for a in audited)


# --- characters that used to corrupt or crash the rebuild ---

def _rebuilt_xml(body_xml, translation):
    src = build_docx(body_xml)
    segs = extract_docx_segments(src)
    rows = [{**s, 'original_text': s['text'], 'translated_text': translation, 'keep_as_is': False,
             'pattern_type': 'mono', 'inline_split': None} for s in segs]
    by_part, meta = build_rebuild_inputs(rows)
    return zipfile.ZipFile(io.BytesIO(rebuild_docx_bytes(src, by_part, meta))).read('word/document.xml').decode()


NBH = '<w:p><w:r><w:t>Ref</w:t><w:noBreakHyphen/><w:t>12</w:t></w:r></w:p>'


def test_a_non_breaking_hyphen_is_part_of_the_extracted_text():
    assert [s['text'] for s in extract_docx_segments(build_docx(NBH))] == ['Ref\u201112']


def test_a_non_breaking_hyphen_is_not_doubled_after_translation():
    xml = _rebuilt_xml(NBH, 'Réf\u201112')
    assert '<w:noBreakHyphen' not in xml
    assert xml.count('\u2011') == 1 and 'Réf\u201112' in xml


def test_control_characters_in_a_translation_do_not_crash_the_rebuild():
    xml = _rebuilt_xml(para('Hello there'), 'Bon\x0bjour\x00 tout')
    assert 'Bonjour tout' in xml
