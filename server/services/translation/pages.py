"""Best-effort .docx segment -> rendered page number mapping.

A .docx has no page concept in its XML; page breaks are computed at layout
time. A job restricts translation to a page range by rendering the document to
PDF with LibreOffice (soffice.py) and matching each segment's text against the
per-page text in document order. The mapping is approximate: it assumes
LibreOffice renders segments in document order, which can drift on complex
table and text-box layouts.
"""

import logging

from .pdf_diff import _import_fitz
from ..soffice import SofficeConversionError, SofficeUnavailable, convert_docx_to_pdf

logger = logging.getLogger(__name__)

_MAX_PAGE = 5000  # beyond any real document; bounds the size of the page set

# Their text is not in reading order with the body (a header repeats on every
# page, a footnote sits at the bottom of the citing page), so the forward-only
# cursor cannot place them.
_UNPLACEABLE_PARTS = ('header', 'footer', 'footnotes', 'endnotes', 'comments')

_PROBE_LEN = 80  # short prefix is enough to locate a segment and more
                 # tolerant of minor whitespace/reflow differences than the full text


def parse_page_spec(spec: str) -> set[int] | None:
    """Parse a page-range specification such as "3-10", "1,4,9" or "1-3,7,12-15".

    :param spec: the specification; blank or None means no filter.
    :return: set of 1-indexed page numbers, or None when there is no filter.
    :raises ValueError: on a non-numeric token, a reversed range, or a page below 1 or above the maximum.
    """
    spec = (spec or '').strip()
    if not spec:
        return None
    pages: set[int] = set()
    for token in spec.split(','):
        token = token.strip()
        if not token:
            continue
        lo, sep, hi = token.partition('-')
        first, last = int(lo), int(hi) if sep else int(lo)
        # A reversed range must raise: an empty set would silently turn the filter off.
        if first < 1 or last < first or last > _MAX_PAGE:
            raise ValueError(f'invalid page range {token!r}')
        pages.update(range(first, last + 1))
    return pages or None


def assign_pages(segments: list[dict], docx_bytes: bytes) -> dict[str, int]:
    """Map segments to rendered page numbers using the document's own LibreOffice PDF rendering.

    A segment missing from the result could not be placed confidently; callers treat it as in range rather than excluding content a mapping failure could not locate.

    :param segments: extracted segment dicts in document order, with "seg_id", "text" and "part".
    :param docx_bytes: the .docx file bytes.
    :return: seg_id to 1-indexed page number; empty when the conversion is unavailable.
    """
    try:
        pdf_bytes = convert_docx_to_pdf(docx_bytes)
    except (SofficeUnavailable, SofficeConversionError) as e:
        logger.warning('page mapping: soffice conversion unavailable, treating whole doc as in-range: %s', e)
        return {}

    fitz = _import_fitz()
    with fitz.open(stream=pdf_bytes, filetype='pdf') as doc:
        # Whitespace-normalised: a probe wrapping onto two PDF lines holds a newline, not a space.
        pages_text = [' '.join(page.get_text().split()) for page in doc]

    result: dict[str, int] = {}
    page_idx = 0
    for seg in segments:
        if any(name in (seg.get('part') or '').rsplit('/', 1)[-1] for name in _UNPLACEABLE_PARTS):
            continue
        text = ' '.join((seg.get('text') or '').split())
        if not text:
            continue
        needle = text[:_PROBE_LEN]
        # Document order tracks non-decreasing page number: search forward only.
        for p in range(page_idx, len(pages_text)):
            if needle in pages_text[p]:
                page_idx = p
                result[seg['seg_id']] = p + 1
                break
        else:
            logger.debug('page mapping: could not place segment %s', seg.get('seg_id'))
    return result
