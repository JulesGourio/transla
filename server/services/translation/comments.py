"""Word-native review comments, injected into an in-memory .docx.

Word comments require four coordinated parts:
  1. word/comments.xml — the comment bodies
  2. document.xml — <w:commentRangeStart/>, <w:commentRangeEnd/>,
     <w:commentReference/> markers around the target paragraph
  3. word/_rels/document.xml.rels — relationship entry for comments.xml
  4. [Content_Types].xml — content type override for comments.xml

review_comments.py generates the comment specs this module injects.
"""
from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from lxml import etree

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
W14_NS = 'http://schemas.microsoft.com/office/word/2010/wordml'
W = f'{{{W_NS}}}'

COMMENTS_REL_TYPE = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments'
COMMENTS_CT = 'application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml'

_XML_DECL = b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
_XML_DECL_FIXED = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _serialize(root) -> bytes:
    """Serialize an XML tree with a double-quoted XML declaration.

    :param root: lxml element to serialize.
    :return: UTF-8 XML bytes.
    """
    raw = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
    return raw.replace(_XML_DECL, _XML_DECL_FIXED, 1)


def _build_comments_xml(comments: list[dict[str, Any]], existing_xml: bytes | None) -> tuple[bytes, dict[str, int]]:
    """Build comments.xml, appending to the document's existing comments when it has some.

    :param comments: comment specs with "seg_id", "comment" and optional "author" and "type".
    :param existing_xml: current word/comments.xml bytes; None when the document has no comments.
    :return: (comments.xml bytes, mapping of seg_id to the Word comment id).
    """
    if existing_xml:
        root = etree.fromstring(existing_xml)
        max_id = max((int(c.get(W + 'id', 0)) for c in root), default=0)
    else:
        root = etree.Element(W + 'comments', nsmap={'w': W_NS, 'w14': W14_NS})
        max_id = 0

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    comment_ids: dict[str, int] = {}

    for i, spec in enumerate(comments):
        cid = max_id + i + 1
        comment_ids[spec['seg_id']] = cid

        comment_el = etree.SubElement(root, W + 'comment')
        comment_el.set(W + 'id', str(cid))
        comment_el.set(W + 'author', spec.get('author', 'Translation review'))
        comment_el.set(W + 'date', now)
        comment_el.set(W + 'initials', 'TR')

        p = etree.SubElement(comment_el, W + 'p')
        r_type = etree.SubElement(p, W + 'r')
        rPr = etree.SubElement(r_type, W + 'rPr')
        etree.SubElement(rPr, W + 'b')
        etree.SubElement(rPr, W + 'sz', attrib={W + 'val': '18'})
        t = etree.SubElement(r_type, W + 't')
        t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        t.text = f"[{spec.get('type', 'review').upper()}] "

        r_text = etree.SubElement(p, W + 'r')
        rPr2 = etree.SubElement(r_text, W + 'rPr')
        etree.SubElement(rPr2, W + 'sz', attrib={W + 'val': '18'})
        t2 = etree.SubElement(r_text, W + 't')
        t2.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        t2.text = spec['comment']

    return _serialize(root), comment_ids


def _add_all_comment_markers(
    doc_root, comments: list[dict[str, Any]], comment_ids: dict[str, int],
    xml_choice_paths: dict[str, list],
) -> int:
    """Wrap each comment's target paragraph in comment range markers and a reference run.

    A path that does not resolve is skipped rather than raised.

    :param doc_root: root element of word/document.xml; mutated in place.
    :param comments: comment specs with "seg_id".
    :param comment_ids: mapping of seg_id to Word comment id.
    :param xml_choice_paths: mapping of seg_id to its positional path, a list of (tag, child index) steps from the root.
    :return: number of comments anchored.
    """
    placed = 0
    for spec in comments:
        sid = spec['seg_id']
        if sid not in comment_ids:
            continue
        path = xml_choice_paths.get(sid)
        if not path:
            continue

        cur = doc_root
        ok = True
        for tag, idx in path:
            children = list(cur)
            if idx >= len(children) or children[idx].tag != tag:
                ok = False
                break
            cur = children[idx]
        if not ok:
            continue

        cid = comment_ids[sid]
        start = etree.Element(W + 'commentRangeStart')
        start.set(W + 'id', str(cid))
        # <w:pPr> must stay the first child of <w:p>, or Word drops the paragraph's numbering.
        insert_idx = 1 if len(cur) and cur[0].tag == W + 'pPr' else 0
        cur.insert(insert_idx, start)

        end = etree.Element(W + 'commentRangeEnd')
        end.set(W + 'id', str(cid))
        cur.append(end)

        ref_run = etree.SubElement(cur, W + 'r')
        ref_rPr = etree.SubElement(ref_run, W + 'rPr')
        etree.SubElement(ref_rPr, W + 'rStyle', attrib={W + 'val': 'CommentReference'})
        ref = etree.SubElement(ref_run, W + 'commentReference')
        ref.set(W + 'id', str(cid))
        placed += 1
    return placed


def _ensure_comments_relationship(rels_xml: bytes) -> bytes:
    """Add the comments relationship to document.xml.rels if missing.

    :param rels_xml: word/_rels/document.xml.rels bytes.
    :return: rels bytes containing the comments relationship.
    """
    root = etree.fromstring(rels_xml)
    ns = 'http://schemas.openxmlformats.org/package/2006/relationships'
    for rel in root:
        if rel.get('Type') == COMMENTS_REL_TYPE:
            return rels_xml
    max_id = 0
    for rel in root:
        rid = rel.get('Id', '')
        if rid.startswith('rId'):
            try:
                max_id = max(max_id, int(rid[3:]))
            except ValueError:
                pass
    new_rel = etree.SubElement(root, f'{{{ns}}}Relationship')
    new_rel.set('Id', f'rId{max_id + 1}')
    new_rel.set('Type', COMMENTS_REL_TYPE)
    new_rel.set('Target', 'comments.xml')
    return _serialize(root)


def _ensure_comments_content_type(ct_xml: bytes) -> bytes:
    """Add the comments content-type override if missing.

    :param ct_xml: [Content_Types].xml bytes.
    :return: content-types bytes containing the comments override.
    """
    root = etree.fromstring(ct_xml)
    ns = 'http://schemas.openxmlformats.org/package/2006/content-types'
    for child in root:
        if child.get('PartName') == '/word/comments.xml':
            return ct_xml
    override = etree.SubElement(root, f'{{{ns}}}Override')
    override.set('PartName', '/word/comments.xml')
    override.set('ContentType', COMMENTS_CT)
    return _serialize(root)


def inject_comments(
    docx_bytes: bytes, comments: list[dict[str, Any]], xml_choice_paths: dict[str, list],
) -> tuple[bytes, int]:
    """Add real Word comments (Review pane) to a rebuilt .docx.

    A comment that cannot be anchored is dropped; this never raises on a bad path.

    :param docx_bytes: the rebuilt .docx.
    :param comments: comment specs [{seg_id, author, type, comment}, ...].
    :param xml_choice_paths: mapping of seg_id to the positional path of the paragraph the comment is about.
    :return: (new .docx bytes, number of comments placed); the input bytes and 0 when there are no comments.
    """
    if not comments:
        return docx_bytes, 0

    with zipfile.ZipFile(BytesIO(docx_bytes), 'r') as zin:
        names = zin.namelist()
        existing_comments = zin.read('word/comments.xml') if 'word/comments.xml' in names else None
        comments_xml, comment_ids = _build_comments_xml(comments, existing_comments)

        doc_root = etree.fromstring(zin.read('word/document.xml'))
        placed = _add_all_comment_markers(doc_root, comments, comment_ids, xml_choice_paths)
        doc_bytes = _serialize(doc_root)

        rels_xml = _ensure_comments_relationship(zin.read('word/_rels/document.xml.rels'))
        ct_xml = _ensure_comments_content_type(zin.read('[Content_Types].xml'))

        out = BytesIO()
        with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                if name == 'word/document.xml':
                    zout.writestr(name, doc_bytes)
                elif name == 'word/_rels/document.xml.rels':
                    zout.writestr(name, rels_xml)
                elif name == '[Content_Types].xml':
                    zout.writestr(name, ct_xml)
                elif name == 'word/comments.xml':
                    continue  # rewritten after the loop
                else:
                    zout.writestr(name, zin.read(name))
            zout.writestr('word/comments.xml', comments_xml)

    return out.getvalue(), placed
