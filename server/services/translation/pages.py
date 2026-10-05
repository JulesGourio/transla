"""Best-effort .docx segment -> rendered page number mapping.

.docx has no native page concept in its XML — page breaks are computed at
layout time (margins, font metrics, manual breaks). To let a job restrict
translation to a page range, we reuse the LibreOffice PDF conversion
already used for previews (soffice.py) and walk its per-page text in
document order, matching each segment's text as we go. This mirrors what a
reviewer would actually see if they opened the doc in Word/a PDF viewer,
but is approximate: it assumes segments appear on the page in the same
order LibreOffice renders them, which can drift on complex table/text-box
layouts.
"""

import logging

from .pdf_diff import _import_fitz
from ..soffice import SofficeConversionError, SofficeUnavailable, convert_docx_to_pdf

logger = logging.getLogger(__name__)

_MAX_PAGE = 5000  # beyond any real document; "1-999999999999" used to build a set of that size

# Their text is not laid out in reading order with the body: a header repeats
# on every page, a footnote sits at the bottom of the page that cites it. The
# forward-only cursor below mapped them all to the last body page, so a filter
# like "1-3" left every header and footnote in the source language.
_UNPLACEABLE_PARTS = ('header', 'footer', 'footnotes', 'endnotes', 'comments')

_PROBE_LEN = 80  # short prefix is enough to locate a segment and more
                 # tolerant of minor whitespace/reflow differences than the full text


def parse_page_spec(spec: str) -> set[int] | None:
    """Parse "3-10", "1,4,9", or "1-3,7,12-15" into a set of 1-indexed page
    numbers. Returns None for a blank spec (= no filter, translate everything)."""
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
        # A reversed range ("5-3") used to come out empty and silently turn the
        # filter off, translating (and paying for) the whole document.
        if first < 1 or last < first or last > _MAX_PAGE:
            raise ValueError(f'invalid page range {token!r}')
        pages.update(range(first, last + 1))
    return pages or None


def assign_pages(segments: list[dict], docx_bytes: bytes) -> dict[str, int]:
    """Return {seg_id: 1-indexed page number} best-effort, via the doc's own
    LibreOffice-rendered PDF. A segment missing from the result could not be
    confidently placed — callers should treat that as "in range" rather than
    silently excluding content a mapping failure merely couldn't locate."""
    try:
        pdf_bytes = convert_docx_to_pdf(docx_bytes)
    except (SofficeUnavailable, SofficeConversionError) as e:
        logger.warning('page mapping: soffice conversion unavailable, treating whole doc as in-range: %s', e)
        return {}

    fitz = _import_fitz()
    with fitz.open(stream=pdf_bytes, filetype='pdf') as doc:
        # Whitespace-normalised: a probe that wraps onto two lines of the PDF
        # contains a newline there, not the space the segment text has.
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
        # Document order should track non-decreasing page number — search
        # forward from the last match, never backward.
        for p in range(page_idx, len(pages_text)):
            if needle in pages_text[p]:
                page_idx = p
                result[seg['seg_id']] = p + 1
                break
        else:
            logger.debug('page mapping: could not place segment %s', seg.get('seg_id'))
    return result
