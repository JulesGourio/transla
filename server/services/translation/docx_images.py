"""OCR + translation of text baked into images embedded in a .docx.

Opt-in: the user picks which images to process, since each one costs a
vision-LLM call. Embedded images are standalone files in the zip
(word/media/imageN.*), so the raw bytes go straight to the vision call.

The image's pixels are never modified: a technical drawing's text is scattered
across the whole image, and resizing it to fit an overlay would distort it
inside its fixed-size Word frame. A translation is a plain paragraph inserted
after the image in the document flow.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import zipfile
from typing import Any, Dict, List, Tuple

from PIL import Image

from ..llm import call_llm_json

logger = logging.getLogger(__name__)

# Prefix of every inserted translation paragraph; the segment_fidelity check
# recognises these paragraphs by it and excludes them.
IMAGE_TRANSLATION_MARKER = '[Traduction]'

_RASTER_EXTS = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff')
_THUMBNAIL_MAX_PX = 160

W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
A_NS = 'http://schemas.openxmlformats.org/drawingml/2006/main'
RELS_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
W = f'{{{W_NS}}}'
R = f'{{{R_NS}}}'
A = f'{{{A_NS}}}'

_OCR_PROMPT = (
    'This image is a crop from a technical aerospace maintenance/assembly '
    'instruction document. Transcribe VERBATIM any text visible in it '
    '(titles, instructions, labels, callouts, part numbers). Preserve line '
    'breaks between distinct text elements (e.g. title vs. body vs. a boxed '
    'label) using \\n. If there is truly no legible text, return an empty '
    'string.\n'
    'Return ONLY a JSON object of the exact shape {"text": "<transcription '
    'or empty string>"} — no extra prose, no markdown fence.'
)


def _make_thumbnail(data: bytes) -> str | None:
    """Build a small base64 JPEG thumbnail of an image.

    :param data: raw image file bytes.
    :return: base64-encoded JPEG text, or None when Pillow cannot open the bytes.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.thumbnail((_THUMBNAIL_MAX_PX, _THUMBNAIL_MAX_PX))
        buf = io.BytesIO()
        img.convert('RGB').save(buf, format='JPEG', quality=70)
        return base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception as e:
        logger.warning('docx_images: could not thumbnail image: %s', e)
        return None


def _vision_payload(name: str, data: bytes) -> Tuple[str, bytes]:
    """Convert an image to a format the vision endpoint accepts.

    PNG and JPEG are sent unchanged; any other format is re-encoded as PNG.

    :param name: media file name, used to pick the format by extension.
    :param data: raw image file bytes.
    :return: (MIME type, image bytes).
    """
    lower = name.lower()
    if lower.endswith('.png'):
        return 'image/png', data
    if lower.endswith(('.jpg', '.jpeg')):
        return 'image/jpeg', data
    buf = io.BytesIO()
    Image.open(io.BytesIO(data)).convert('RGB').save(buf, format='PNG')
    return 'image/png', buf.getvalue()


def _content_hash(data: bytes) -> str:
    """Hash image bytes for exact-duplicate grouping.

    An exact hash is enough because a reused picture (a repeated logo) is a byte-identical media file, and it never merges two different diagrams that merely look alike.

    :param data: raw image file bytes.
    :return: hex SHA-256 digest.
    """
    return hashlib.sha256(data).hexdigest()


def list_docx_images(docx_bytes: bytes) -> List[Dict[str, Any]]:
    """List the embedded raster images of a .docx with a thumbnail for a selection UI.

    Each entry carries a content "hash" and a "repeat_count" (how many embedded images are byte-identical to it), so the UI can badge repeated graphics. Non-raster media (.emf/.wmf) are skipped.

    :param docx_bytes: the .docx file bytes.
    :return: dicts with filename, media_path, size_bytes, thumbnail_base64, hash and repeat_count, sorted by media path.
    """
    out: List[Dict[str, Any]] = []
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        media_names = sorted(n for n in z.namelist() if n.startswith('word/media/'))
        for name in media_names:
            if not name.lower().endswith(_RASTER_EXTS):
                continue
            data = z.read(name)
            thumb_b64 = _make_thumbnail(data)
            if thumb_b64 is None:
                continue
            out.append({
                'filename': name.rsplit('/', 1)[-1],
                'media_path': name,
                'size_bytes': len(data),
                'thumbnail_base64': thumb_b64,
                'hash': _content_hash(data),
            })

    hash_counts: Dict[str, int] = {}
    for img in out:
        hash_counts[img['hash']] = hash_counts.get(img['hash'], 0) + 1
    for img in out:
        img['repeat_count'] = hash_counts[img['hash']]
    return out


async def ocr_docx_images(
    docx_bytes: bytes, selected_filenames: List[str], host: str, token: str, endpoint: str,
) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
    """Transcribe the selected embedded images with a vision LLM.

    Images are grouped by exact content hash: one LLM call per unique image, and every selected filename sharing that hash gets the same transcription. A failed or empty transcription skips that group and never raises.

    :param docx_bytes: the .docx file bytes.
    :param selected_filenames: media file names (without directory) to transcribe.
    :param host: Databricks workspace host.
    :param token: access token for the LLM endpoint.
    :param endpoint: serving endpoint name of the vision model.
    :return: (results, usage_log); results are dicts with filename, media_path, text and thumbnail_base64; usage_log has one {filename, usage} entry per call that reached the endpoint.
    """
    selected = set(selected_filenames)
    # One representative per content hash, plus every selected filename sharing it.
    groups: Dict[str, Dict[str, Any]] = {}
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        for name in z.namelist():
            if not name.startswith('word/media/'):
                continue
            filename = name.rsplit('/', 1)[-1]
            if filename not in selected:
                continue
            data = z.read(name)
            h = _content_hash(data)
            group = groups.setdefault(h, {'filenames': [], 'representative': None})
            group['filenames'].append(filename)
            if group['representative'] is None:
                group['representative'] = {'filename': filename, 'name': name, 'data': data}

    results: List[Dict[str, str]] = []
    usage_log: List[Dict[str, Any]] = []
    for h, group in groups.items():
        rep = group['representative']
        try:
            mime, payload = _vision_payload(rep['name'], rep['data'])
        except Exception as e:
            logger.warning('docx_images: could not prepare %s for transcription: %s', rep['filename'], e)
            continue
        b64 = base64.b64encode(payload).decode('ascii')
        messages = [{'role': 'user', 'content': [
            {'type': 'text', 'text': _OCR_PROMPT},
            {'type': 'image_url', 'image_url': {'url': f'data:{mime};base64,{b64}'}},
        ]}]
        try:
            result, usage = await call_llm_json(host, token, endpoint, messages, max_tokens=1024)
        except Exception as e:
            logger.warning('docx_images: transcription failed for %s (hash shared by %d file(s)): %s',
                            rep['filename'], len(group['filenames']), e)
            continue
        usage_log.append({'filename': rep['filename'], 'usage': usage})
        text = (result.get('text') or '').strip()
        if not text:
            continue
        thumb_b64 = _make_thumbnail(rep['data'])
        for filename in group['filenames']:
            results.append({
                'filename': filename, 'media_path': f'word/media/{filename}', 'text': text,
                'thumbnail_base64': thumb_b64,
            })
    return results, usage_log


def _local_name(tag: str) -> str:
    """Strip the namespace from an XML tag.

    :param tag: tag in Clark notation ("{ns}name") or plain.
    :return: the local tag name.
    """
    return tag.rsplit('}', 1)[-1] if '}' in tag else tag


def _load_image_rels(z: zipfile.ZipFile) -> Dict[str, str]:
    """Map relationship ids to media targets for the image relationships of the main document.

    :param z: open .docx archive.
    :return: rId to "word/media/..." path; empty when the document has no relationships part.
    """
    from lxml import etree as lxml_etree

    rels_path = 'word/_rels/document.xml.rels'
    if rels_path not in z.namelist():
        return {}
    root = lxml_etree.fromstring(z.read(rels_path))
    out = {}
    for rel in root:
        if not rel.get('Type', '').endswith('/image'):
            continue
        target = rel.get('Target', '')
        rid = rel.get('Id', '')
        if rid and target:
            out[rid] = f"word/{target.lstrip('/')}" if not target.startswith('word/') else target
    return out


def find_image_anchors(docx_bytes: bytes, filenames: List[str]) -> Dict[str, int]:
    """Locate the body position hosting each requested image.

    The position is the 0-based index, among all direct children of <w:body>, of the paragraph or table containing the image's <a:blip>; it matches extract.py's body_p_idx, and a picture in a table cell resolves to the table's index. Only word/document.xml is scanned.

    :param docx_bytes: the .docx file bytes.
    :param filenames: media file names (without directory) to locate.
    :return: file name to body index; images without a resolvable anchor (e.g. inside a text box) are absent.
    """
    from lxml import etree as lxml_etree

    wanted = set(filenames)
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        if 'word/document.xml' not in z.namelist():
            return {}
        rid_to_media = _load_image_rels(z)
        root = lxml_etree.fromstring(z.read('word/document.xml'))

    body = next((c for c in root if _local_name(c.tag) == 'body'), None)
    if body is None:
        return {}
    body_children = list(body)

    anchors: Dict[str, int] = {}
    for blip in root.iter(f'{A}blip'):
        rid = blip.get(f'{R}embed')
        if not rid or rid not in rid_to_media:
            continue
        filename = rid_to_media[rid].rsplit('/', 1)[-1]
        if filename not in wanted or filename in anchors:
            continue
        node = blip
        top = None
        while node is not None:
            parent = node.getparent()
            if parent is body:
                top = node
                break
            node = parent
        if top is None:
            continue
        try:
            anchors[filename] = body_children.index(top)
        except ValueError:
            continue
    return anchors


def order_ocr_segments(
    base_segments: List[Dict[str, Any]],
    ocr_segments: List[Dict[str, Any]],
    anchors: Dict[str, int],
) -> List[Dict[str, Any]]:
    """Splice OCR'd image segments into the extracted segments near their image's body position.

    Segment order is the only document-order signal kept in the database, so it decides where an image appears in the Segments panel.

    :param base_segments: extracted segments, in document order.
    :param ocr_segments: image segment dicts whose xml_choice_path holds the "media_filename".
    :param anchors: file name to body index, from find_image_anchors.
    :return: a new list; image segments without an anchor are appended at the end.
    """
    result = list(base_segments)
    to_place: List[Tuple[int, Dict[str, Any]]] = []
    tail: List[Dict[str, Any]] = []
    for seg in ocr_segments:
        filename = seg['xml_choice_path']['media_filename']
        anchor = anchors.get(filename)
        if anchor is None:
            tail.append(seg)
        else:
            to_place.append((anchor, seg))
    to_place.sort(key=lambda pair: pair[0])

    last_anchor = None
    insert_at = 0
    for anchor, seg in to_place:
        if anchor != last_anchor:
            insert_at = next(
                (i for i, s in enumerate(result) if s.get('body_p_idx') is not None and s['body_p_idx'] > anchor),
                len(result),
            )
            last_anchor = anchor
        result.insert(insert_at, seg)
        insert_at += 1

    result.extend(tail)
    return result


def insert_image_translations(docx_bytes: bytes, translations: Dict[str, str]) -> bytes:
    """Insert a visible italic paragraph after each translated image's own paragraph in word/document.xml.

    Every other part of the archive is copied byte-for-byte.

    :param docx_bytes: the .docx file bytes.
    :param translations: media file name (e.g. "image4.jpg") to translated text; line breaks become <w:br/>.
    :return: the new .docx bytes, or the input unchanged when there is nothing to insert.
    """
    from lxml import etree as lxml_etree

    with zipfile.ZipFile(io.BytesIO(docx_bytes), 'r') as zin:
        names = zin.namelist()
        doc_path = 'word/document.xml'
        if doc_path not in names or not translations:
            return docx_bytes

        rid_to_media = _load_image_rels(zin)
        by_filename = translations

        parser = lxml_etree.XMLParser(remove_blank_text=False, strip_cdata=False)
        root = lxml_etree.fromstring(zin.read(doc_path), parser)

        # Collected first: mutating the tree while iterating it is unsafe.
        to_insert: List[Tuple[Any, str]] = []
        seen_media: set = set()
        for blip in root.iter(f'{A}blip'):
            rid = blip.get(f'{R}embed')
            if not rid or rid not in rid_to_media:
                continue
            media_path = rid_to_media[rid]
            filename = media_path.rsplit('/', 1)[-1]
            text = by_filename.get(filename)
            if not text or media_path in seen_media:
                continue
            # Nearest ancestor <w:p>, whatever the drawing's nesting inside mc:AlternateContent.
            p = blip
            while p is not None and _local_name(p.tag) != 'p':
                p = p.getparent()
            if p is None:
                continue
            seen_media.add(media_path)
            to_insert.append((p, text))

        for p, text in to_insert:
            new_p = lxml_etree.Element(f'{W}p')
            p.addnext(new_p)
            r = lxml_etree.SubElement(new_p, f'{W}r')
            rpr_run = lxml_etree.SubElement(r, f'{W}rPr')
            lxml_etree.SubElement(rpr_run, f'{W}i')
            # A literal newline inside <w:t> is not a line break in OOXML; each line needs its own <w:t> and an explicit <w:br/>.
            lines = [f'{IMAGE_TRANSLATION_MARKER} {line}' if i == 0 else line
                     for i, line in enumerate(text.split('\n'))]
            for i, line in enumerate(lines):
                if i > 0:
                    lxml_etree.SubElement(r, f'{W}br')
                t = lxml_etree.SubElement(r, f'{W}t')
                t.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
                t.text = line

        raw = lxml_etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
        raw = raw.replace(
            b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>",
            b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', 1,
        )
        raw = raw.replace(b'?>\n<', b'?>\r\n<', 1)

        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                zout.writestr(name, raw if name == doc_path else zin.read(name))

    return out_buf.getvalue()
