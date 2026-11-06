"""Pixel-level page diff for the Translate tab's preview: the grayscale
original with red highlights over pixel-level changes.
"""
from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageChops, ImageOps


def _import_fitz() -> Any:
    """Import PyMuPDF under either of its module names.

    :return: the PyMuPDF module.
    :raises ImportError: when PyMuPDF is not installed.
    """
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24
        return fitz
    except ImportError:
        pass
    try:
        import fitz  # PyMuPDF < 1.24
        return fitz
    except ImportError as e:
        raise ImportError(
            f'PyMuPDF could not be imported ({e}). '
            'Ensure "pymupdf>=1.25.0" is in pyproject.toml and the app has been redeployed.'
        ) from e


_DIFF_THRESHOLD = 25  # grayscale delta above which a pixel counts as "changed"
_HIGHLIGHT_RGB = (220, 38, 38)


def _render_page_from_doc(fitz_module: Any, doc: Any, page_num: int, zoom: float) -> Image.Image | None:
    """Render one page of an open PDF.

    :param fitz_module: the PyMuPDF module.
    :param doc: open PyMuPDF document.
    :param page_num: 1-indexed page number.
    :param zoom: scale factor of the rendering.
    :return: RGB image, or None when page_num is out of range.
    """
    if page_num < 1 or page_num > doc.page_count:
        return None
    page = doc[page_num - 1]
    pix = page.get_pixmap(matrix=fitz_module.Matrix(zoom, zoom))
    return Image.frombytes('RGB', (pix.width, pix.height), pix.samples)


def _render_page(fitz_module: Any, pdf_bytes: bytes, page_num: int, zoom: float) -> tuple[Image.Image | None, int]:
    """Render one page of a PDF given as bytes.

    :param fitz_module: the PyMuPDF module.
    :param pdf_bytes: the PDF file bytes.
    :param page_num: 1-indexed page number.
    :param zoom: scale factor of the rendering.
    :return: (RGB image or None when page_num is out of range, total page count).
    """
    doc = fitz_module.open(stream=pdf_bytes, filetype='pdf')
    try:
        return _render_page_from_doc(fitz_module, doc, page_num, zoom), doc.page_count
    finally:
        doc.close()


def _pad_to_match(a: Image.Image, b: Image.Image) -> tuple[Image.Image, Image.Image]:
    """Pad two images with a white background to their shared maximum size, without resizing.

    :param a: first image.
    :param b: second image.
    :return: the two padded images.
    """
    w, h = max(a.width, b.width), max(a.height, b.height)

    def _pad(img: Image.Image) -> Image.Image:
        """Pad one image to the enclosing canvas size.

        :param img: image to pad.
        :return: the image itself when it already has the canvas size, otherwise a padded copy.
        """
        if img.size == (w, h):
            return img
        canvas = Image.new('RGB', (w, h), 'white')
        canvas.paste(img, (0, 0))
        return canvas

    return _pad(a), _pad(b)


def _diff_mask(before_img: Image.Image, after_img: Image.Image) -> tuple[Image.Image, Image.Image, Image.Image]:
    """Compute the grayscale difference mask of two page images.

    :param before_img: image of the original page.
    :param after_img: image of the translated page.
    :return: (padded before, padded after, mask) where the mask is an "L" image, 255 for changed pixels and 0 otherwise.
    """
    before_img, after_img = _pad_to_match(before_img, after_img)
    gray_before = ImageOps.grayscale(before_img)
    gray_after = ImageOps.grayscale(after_img)
    diff = ImageChops.difference(gray_before, gray_after)
    mask = diff.point(lambda p: 255 if p > _DIFF_THRESHOLD else 0)
    return before_img, after_img, mask


def _composite_diff(before_img: Image.Image, after_img: Image.Image) -> Image.Image:
    """Draw the original page in grayscale with red highlights wherever the other page differs.

    :param before_img: image of the original page.
    :param after_img: image of the translated page.
    :return: the composite RGB image.
    """
    before_img, _after_img, mask = _diff_mask(before_img, after_img)
    out = ImageOps.grayscale(before_img).convert('RGB')
    highlight = Image.new('RGB', out.size, _HIGHLIGHT_RGB)
    return Image.composite(highlight, out, mask)


_CHANGE_CELL_PX = 24  # grid cell size for connected-component grouping
_CHANGE_CELL_MIN_FRAC = 0.06  # fraction of sampled cell pixels that must differ to count the cell as "changed"


def _detect_change_regions(mask: Image.Image) -> list[tuple[float, float, float, float]]:
    """Group changed pixels into bounding boxes by connected components on a coarse grid.

    :param mask: "L" difference mask, 255 for changed pixels.
    :return: (x0, y0, x1, y1) boxes as fractions of the image width and height, in reading order.
    """
    w, h = mask.size
    cols = max(1, w // _CHANGE_CELL_PX)
    rows = max(1, h // _CHANGE_CELL_PX)
    px = mask.load()
    changed = [[False] * cols for _ in range(rows)]
    for ry in range(rows):
        y0, y1 = ry * _CHANGE_CELL_PX, min((ry + 1) * _CHANGE_CELL_PX, h)
        for rx in range(cols):
            x0, x1 = rx * _CHANGE_CELL_PX, min((rx + 1) * _CHANGE_CELL_PX, w)
            sampled = 0
            hit = 0
            for yy in range(y0, y1, 2):
                for xx in range(x0, x1, 2):
                    sampled += 1
                    if px[xx, yy] > 0:
                        hit += 1
            if sampled and hit / sampled > _CHANGE_CELL_MIN_FRAC:
                changed[ry][rx] = True

    visited = [[False] * cols for _ in range(rows)]
    regions: list[tuple[float, float, float, float]] = []
    for ry in range(rows):
        for rx in range(cols):
            if not changed[ry][rx] or visited[ry][rx]:
                continue
            visited[ry][rx] = True
            stack = [(ry, rx)]
            min_r = max_r = ry
            min_c = max_c = rx
            while stack:
                cy, cx = stack.pop()
                min_r, max_r = min(min_r, cy), max(max_r, cy)
                min_c, max_c = min(min_c, cx), max(max_c, cx)
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < rows and 0 <= nx < cols and changed[ny][nx] and not visited[ny][nx]:
                        visited[ny][nx] = True
                        stack.append((ny, nx))
            regions.append((min_c / cols, min_r / rows, (max_c + 1) / cols, (max_r + 1) / rows))

    regions.sort(key=lambda b: (b[1], b[0]))  # reading order: top to bottom, left to right
    return regions


def get_all_page_changes(
    pdf_before: bytes, pdf_after: bytes, zoom: float = 1.5,
) -> list[dict]:
    """List the change regions of every page, for cross-page next/previous navigation.

    :param pdf_before: original PDF bytes.
    :param pdf_after: translated PDF bytes.
    :param zoom: scale factor of the rendering.
    :return: [{"page": N, "bbox": [x0, y0, x1, y1]}, ...] in reading order, over the pages both PDFs have.
    """
    fitz = _import_fitz()
    doc_before = fitz.open(stream=pdf_before, filetype='pdf')
    doc_after = fitz.open(stream=pdf_after, filetype='pdf')
    try:
        total = min(doc_before.page_count, doc_after.page_count)
        out: list[dict] = []
        for n in range(1, total + 1):
            before_img = _render_page_from_doc(fitz, doc_before, n, zoom)
            after_img = _render_page_from_doc(fitz, doc_after, n, zoom)
            _b, _a, mask = _diff_mask(before_img, after_img)
            for bbox in _detect_change_regions(mask):
                out.append({'page': n, 'bbox': list(bbox)})
        return out
    finally:
        doc_before.close()
        doc_after.close()


def render_page_diff(
    pdf_before: bytes, pdf_after: bytes, page_num: int, zoom: float = 1.5,
) -> tuple[bytes | None, int]:
    """Render one page of the original with red highlights where the translated page differs.

    :param pdf_before: original PDF bytes.
    :param pdf_after: translated PDF bytes.
    :param page_num: 1-indexed page number.
    :param zoom: scale factor of the rendering.
    :return: (PNG bytes or None when page_num is out of range for either document, the smaller of the two page counts).
    """
    fitz = _import_fitz()
    before_img, before_pages = _render_page(fitz, pdf_before, page_num, zoom)
    after_img, after_pages = _render_page(fitz, pdf_after, page_num, zoom)
    total = min(before_pages, after_pages)
    if before_img is None or after_img is None:
        return None, total

    out = _composite_diff(before_img, after_img)
    buf = io.BytesIO()
    out.save(buf, format='PNG')
    return buf.getvalue(), total


