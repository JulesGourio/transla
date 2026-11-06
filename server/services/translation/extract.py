"""
extract.py - Deep-structured extraction of translatable segments from a .docx.

For each text-bearing XML part in the .docx, walks the structure and emits
one Segment per translatable paragraph. Handles:

  - Body paragraphs (direct text in <w:p>)
  - Tables (<w:tbl>/<w:tr>/<w:tc>), with row/cell coordinates
  - Drawing text boxes (<w:txbxContent> inside <mc:AlternateContent>),
    descending only into mc:Choice but recording the matching mc:Fallback
    path so rebuild can update both copies.
  - Structured document tags (<w:sdt> - including TOC content controls),
    treated as a transparent container.

For each paragraph we also record:
  - Run-level formatting fingerprint (fmt_hash per run, fmt_signature per
    paragraph) used by audit.py to identify primary vs secondary language
    by formatting alone.
  - A positional XML path (list of (tag, child_idx) tuples) from the part
    root to the paragraph element. rebuild.py uses this path to locate the
    exact node to modify - more reliable than re-parsing or XPath.

Usage:  python extract.py <input.docx> <output.json>
"""

from __future__ import annotations
import json
import logging
import sys
import zipfile
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
W = f"{{{W_NS}}}"
MC = f"{{{MC_NS}}}"

# Tags we should NOT descend into when collecting direct runs of a paragraph,
# because they contain text that we extract separately as its own segment(s).
SKIP_DESCENT_TAGS = {
    W + "drawing",
    MC + "AlternateContent",
    W + "pict",
}

# footnotes.xml/endnotes.xml/comments.xml wrap each note's paragraphs one level
# deeper than document.xml's body, so extract_part must descend into these.
NOTE_WRAPPER_TAGS = {W + "footnote", W + "endnote", W + "comment"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def discover_text_parts(zip_names: list[str]) -> list[str]:
    """List the XML parts of a .docx that may contain user text.

    :param zip_names: member names of the .docx archive.
    :return: part names (document, headers, footers, footnotes, endnotes, comments) in reading order.
    """
    candidates = []
    for name in zip_names:
        if not name.startswith("word/"):
            continue
        if not name.endswith(".xml"):
            continue
        base = name.rsplit("/", 1)[-1]
        # Match: document, headerN, footerN, footnotes, endnotes, comments
        if (base == "document.xml"
                or re.fullmatch(r"header\d+\.xml", base)
                or re.fullmatch(r"footer\d+\.xml", base)
                or base in ("footnotes.xml", "endnotes.xml", "comments.xml")):
            candidates.append(name)
    def sort_key(n: str) -> tuple:
        """Sort key putting document first, then headers, footers, footnotes, endnotes, comments, numbered parts in numeric order.

        :param n: part name.
        :return: (priority, number in the name, name).
        """
        b = n.rsplit("/", 1)[-1]
        priority = (
            0 if b == "document.xml"
            else 1 if b.startswith("header")
            else 2 if b.startswith("footer")
            else 3 if b == "footnotes.xml"
            else 4 if b == "endnotes.xml"
            else 5
        )
        # Numeric, not lexicographic: "header10.xml" must follow "header2.xml".
        m = re.search(r"\d+", b)
        num = int(m.group()) if m else 0
        return (priority, num, b)
    return sorted(candidates, key=sort_key)


def build_parent_map(root: ET.Element) -> dict:
    """Map each element to its parent.

    :param root: root of the XML tree.
    :return: dict from id(child) to its parent element.
    """
    parent_map = {}
    for parent in root.iter():
        for child in parent:
            parent_map[id(child)] = parent
    return parent_map


def positional_path(elem: ET.Element, root: ET.Element, parent_map: dict) -> list:
    """Compute the positional path from the root to an element.

    :param elem: target element.
    :param root: root of the tree containing elem.
    :param parent_map: result of build_parent_map for that tree.
    :return: [[tag, child index], ...] from the root down; empty for the root itself, and truncated if elem is not in the tree.
    """
    if elem is root:
        return []
    path = []
    cur = elem
    while cur is not root:
        parent = parent_map.get(id(cur))
        if parent is None:
            # elem isn't actually in this tree; return what we have
            break
        # Find child index by identity (not equality - elements may compare equal)
        idx = next((i for i, c in enumerate(parent) if c is cur), None)
        if idx is None:
            break
        path.append([cur.tag, idx])
        cur = parent
    path.reverse()
    return path


def fmt_hash(rPr: ET.Element | None) -> str:
    """Hash the formatting properties of a run that distinguish languages or convey real formatting (size, bold, italic, underline, color, font, language, vertical alignment, highlight, strike).

    :param rPr: the run's <w:rPr> element; None for an unformatted run.
    :return: deterministic compact fingerprint; "" when rPr is None.
    """
    if rPr is None:
        return ""
    interesting = {"sz", "szCs", "b", "bCs", "i", "iCs", "u", "color",
                   "rFonts", "lang", "vertAlign", "highlight", "strike"}
    parts = []
    for child in rPr:
        tag = child.tag.split("}", 1)[-1]
        if tag not in interesting:
            continue
        attrs = ",".join(f"{k.split('}',1)[-1]}={v}"
                         for k, v in sorted(child.attrib.items()))
        parts.append(f"{tag}({attrs})" if attrs else tag)
    return "|".join(sorted(parts))


def walk_skip_drawings(elem: ET.Element) -> Iterator[ET.Element]:
    """Yield an element and its descendants, skipping drawing, text-box and pict subtrees, which are extracted separately.

    :param elem: element to start from.
    :return: iterator over the visited elements.
    """
    yield elem
    for child in elem:
        if child.tag in SKIP_DESCENT_TAGS:
            continue
        yield from walk_skip_drawings(child)


def collect_direct_runs(p: ET.Element) -> tuple[str, list[dict]]:
    """Collect the text runs directly in a paragraph, excluding drawings and text boxes.

    Tabs, line breaks and non-breaking hyphens become "\t", "\n" and U+2011 so the text keeps them.

    :param p: <w:p> element.
    :return: (concatenated text, run dicts with idx, text and fmt_hash).
    """
    runs = []
    idx = 0
    for elem in walk_skip_drawings(p):
        if elem.tag != W + "r":
            continue
        rPr = elem.find(W + "rPr")
        h = fmt_hash(rPr)
        for t in elem:
            tag = t.tag
            if tag == W + "t":
                txt = t.text or ""
                if txt:
                    runs.append({"idx": idx, "text": txt, "fmt_hash": h})
                    idx += 1
            elif tag == W + "tab":
                runs.append({"idx": idx, "text": "\t", "fmt_hash": h})
                idx += 1
            elif tag == W + "br":
                runs.append({"idx": idx, "text": "\n", "fmt_hash": h})
                idx += 1
            elif tag == W + "noBreakHyphen":
                # Must be read as a character: dropping it would turn "Ref-12" into "Ref12" for the LLM.
                runs.append({"idx": idx, "text": "\u2011", "fmt_hash": h})
                idx += 1
    text = "".join(r["text"] for r in runs)
    return text, runs


def dominant_fmt_signature(runs: list[dict]) -> str:
    """Pick the most common non-blank format hash of runs, weighted by character count.

    :param runs: run dicts with "text" and "fmt_hash".
    :return: the format hash; "" when there are no runs, the first run's hash when all text is blank.
    """
    if not runs:
        return ""
    weights: Counter = Counter()
    for r in runs:
        if r["text"].strip():
            weights[r["fmt_hash"]] += len(r["text"])
    if not weights:
        return runs[0]["fmt_hash"]
    return weights.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Segment construction
# ---------------------------------------------------------------------------

def make_segment(*, seg_id: str, part: str, location_type: str, body_p_idx: int | str,
                 table_coords: dict | None = None, txbx_path: dict | None = None,
                 para_idx_in_container: int, text: str, runs: list[dict],
                 xml_choice_path: list, xml_fallback_path: list | None = None) -> dict:
    """Build a segment dict.

    :param seg_id: unique, human-readable segment id.
    :param part: XML part name the paragraph belongs to.
    :param location_type: "body_direct", "table", "sdt" or "txbx".
    :param body_p_idx: index of the top-level body child hosting the paragraph.
    :param table_coords: table, row and cell coordinates for table paragraphs; None otherwise.
    :param txbx_path: text-box coordinates for text-box paragraphs; None otherwise.
    :param para_idx_in_container: paragraph index within its cell, text box or container.
    :param text: concatenated paragraph text.
    :param runs: run dicts of the paragraph.
    :param xml_choice_path: positional path of the paragraph element.
    :param xml_fallback_path: positional path of the twin paragraph in mc:Fallback; None when there is none.
    :return: segment dict, including the dominant format signature.
    """
    return {
        "seg_id": seg_id,
        "part": part,
        "location_type": location_type,
        "body_p_idx": body_p_idx,
        "table_coords": table_coords,
        "txbx_path": txbx_path,
        "para_idx_in_container": para_idx_in_container,
        "text": text,
        "runs": runs,
        "fmt_signature": dominant_fmt_signature(runs),
        "xml_choice_path": xml_choice_path,
        "xml_fallback_path": xml_fallback_path,
    }


# ---------------------------------------------------------------------------
# Extractors per location type
# ---------------------------------------------------------------------------

def extract_paragraph_direct(p: ET.Element, *, part: str, body_p_idx: int,
                             location_type: str, root: ET.Element,
                             parent_map: dict,
                             para_idx_in_container: int,
                             table_coords: dict | None = None) -> list[dict]:
    """Extract the text directly contained in a paragraph (not its drawings).

    :param p: <w:p> element.
    :param part: XML part name.
    :param body_p_idx: index of the top-level body child hosting the paragraph.
    :param location_type: "body_direct", "table" or "sdt".
    :param root: root of the part's tree.
    :param parent_map: result of build_parent_map for that tree.
    :param para_idx_in_container: paragraph index within its container.
    :param table_coords: table coordinates when location_type is "table".
    :return: one segment, or an empty list when the paragraph has no text.
    """
    text, runs = collect_direct_runs(p)
    if not text.strip():
        return []
    if location_type == "table":
        tc = table_coords or {}
        seg_id = (f"{part}#table.bt{tc.get('table_body_idx',body_p_idx)}"
                  f".r{tc.get('row_idx','?')}.c{tc.get('cell_idx','?')}"
                  f"#p{para_idx_in_container}")
    elif location_type == "sdt":
        seg_id = f"{part}#sdt.bp{body_p_idx}#p{para_idx_in_container}"
    else:
        seg_id = f"{part}#body_direct.bp{body_p_idx}#p{para_idx_in_container}"
    return [make_segment(
        seg_id=seg_id,
        part=part,
        location_type=location_type,
        body_p_idx=body_p_idx,
        table_coords=table_coords,
        para_idx_in_container=para_idx_in_container,
        text=text, runs=runs,
        xml_choice_path=positional_path(p, root, parent_map),
    )]


def _inside_alternate_content(elem: ET.Element, host: ET.Element, parent_map: dict) -> bool:
    """Tell whether an element sits under an mc:AlternateContent between itself and a host.

    :param elem: element to test.
    :param host: ancestor at which the upward search stops.
    :param parent_map: result of build_parent_map.
    :return: True when an AlternateContent ancestor is found below the host.
    """
    cur = parent_map.get(id(elem))
    while cur is not None and cur is not host:
        if cur.tag == MC + "AlternateContent":
            return True
        cur = parent_map.get(id(cur))
    return False


def _txbx_segments(txbx: ET.Element, fb_txbx: ET.Element | None, *, part: str,
                   body_p_idx: int, tag: str, root: ET.Element,
                   parent_map: dict, txbx_path_base: dict) -> list[dict]:
    """Extract one segment per paragraph of a text box, plus the cells of any table in it.

    :param txbx: <w:txbxContent> element.
    :param fb_txbx: the matching mc:Fallback copy; None for a box with no twin.
    :param part: XML part name.
    :param body_p_idx: index of the top-level body child hosting the box.
    :param tag: suffix identifying the box inside seg_ids.
    :param root: root of the part's tree.
    :param parent_map: result of build_parent_map for that tree.
    :param txbx_path_base: base text-box coordinates stored on each segment.
    :return: segment dicts.
    """
    segments = []
    container = "/".join(f"{t.rsplit('}', 1)[-1]}:{i}"
                         for t, i in positional_path(txbx, root, parent_map))
    fb_paras = fb_txbx.findall(W + "p") if fb_txbx is not None else []
    for p_idx, txp in enumerate(txbx.findall(W + "p")):
        text, runs = collect_direct_runs(txp)
        if not text.strip():
            continue
        fb_path = None
        if p_idx < len(fb_paras):
            fb_path = positional_path(fb_paras[p_idx], root, parent_map)
        segments.append(make_segment(
            seg_id=f"{part}#txbx.bp{body_p_idx}.{tag}#p{p_idx}",
            part=part,
            location_type="txbx",
            body_p_idx=body_p_idx,
            # `container` is the box's own XML position; the other fields repeat across cells and content controls.
            txbx_path={**txbx_path_base, "para_idx": p_idx, "container": container},
            para_idx_in_container=p_idx,
            text=text, runs=runs,
            xml_choice_path=positional_path(txp, root, parent_map),
            xml_fallback_path=fb_path,
        ))
    for t_idx, tbl in enumerate(txbx.findall(W + "tbl")):
        table_segs = extract_table(
            tbl, part=part, body_p_idx=f"{body_p_idx}.{tag}.t{t_idx}",
            root=root, parent_map=parent_map,
        )
        if fb_txbx is not None:
            ch_prefix = positional_path(txbx, root, parent_map)
            fb_prefix = positional_path(fb_txbx, root, parent_map)
            for seg in table_segs:
                # The Fallback copy mirrors the Choice one, so the same relative position points at its twin.
                seg["xml_fallback_path"] = fb_prefix + seg["xml_choice_path"][len(ch_prefix):]
        segments.extend(table_segs)
    return segments


def extract_drawings_in(host: ET.Element, *, part: str, body_p_idx: int,
                        root: ET.Element, parent_map: dict) -> list[dict]:
    """Extract the text of every text box under a paragraph or table cell.

    Each mc:AlternateContent is read through mc:Choice and its mc:Fallback twin (same ordinal) is recorded in xml_fallback_path. A text box outside any AlternateContent (legacy VML, bare drawing) has no twin.

    :param host: <w:p> or table cell element.
    :param part: XML part name.
    :param body_p_idx: index of the top-level body child hosting the element.
    :param root: root of the part's tree.
    :param parent_map: result of build_parent_map for that tree.
    :return: segment dicts.
    """
    segments = []
    # iter() finds AlternateContent at any depth, since wrappers like mc:Choice/wp:inline nest.
    for ac_idx, ac in enumerate(host.iter(MC + "AlternateContent")):
        choice = ac.find(MC + "Choice")
        fallback = ac.find(MC + "Fallback")
        if choice is None:
            continue

        choice_txbxes = list(choice.iter(W + "txbxContent"))
        fallback_txbxes = list(fallback.iter(W + "txbxContent")) if fallback is not None else []

        for tx_idx, txbx in enumerate(choice_txbxes):
            fb_txbx = fallback_txbxes[tx_idx] if tx_idx < len(fallback_txbxes) else None
            segments.extend(_txbx_segments(
                txbx, fb_txbx, part=part, body_p_idx=body_p_idx,
                tag=f"ac{ac_idx}.tx{tx_idx}", root=root, parent_map=parent_map,
                txbx_path_base={"body_p_idx": body_p_idx, "ac_idx": ac_idx, "txbx_idx": tx_idx},
            ))

    bare = [t for t in host.iter(W + "txbxContent") if not _inside_alternate_content(t, host, parent_map)]
    for k, txbx in enumerate(bare):
        segments.extend(_txbx_segments(
            txbx, None, part=part, body_p_idx=body_p_idx,
            tag=f"v{k}", root=root, parent_map=parent_map,
            txbx_path_base={"body_p_idx": body_p_idx, "ac_idx": f"v{k}", "txbx_idx": 0},
        ))
    return segments


def extract_table(tbl: ET.Element, *, part: str, body_p_idx: int | str,
                  root: ET.Element, parent_map: dict) -> list[dict]:
    """Extract every paragraph of a table, descending into nested tables, content controls and text boxes.

    :param tbl: <w:tbl> element.
    :param part: XML part name.
    :param body_p_idx: body index of a top-level table (int), or a compound string id for a nested table; it only feeds the readable seg_id.
    :param root: root of the part's tree.
    :param parent_map: result of build_parent_map for that tree.
    :return: segment dicts.
    """
    segments = []
    for row_idx, tr in enumerate(tbl.findall(W + "tr")):
        for cell_idx, tc in enumerate(tr.findall(W + "tc")):
            tcoords = {"table_body_idx": body_p_idx,
                       "row_idx": row_idx, "cell_idx": cell_idx}
            # Direct children in document order, so nested tables and content controls are reached.
            p_idx = 0
            for child in tc:
                if child.tag == W + "p":
                    segments.extend(extract_paragraph_direct(
                        child, part=part, body_p_idx=body_p_idx,
                        location_type="table", root=root, parent_map=parent_map,
                        para_idx_in_container=p_idx,
                        table_coords=tcoords,
                    ))
                    segments.extend(extract_drawings_in(
                        child, part=part, body_p_idx=body_p_idx,
                        root=root, parent_map=parent_map,
                    ))
                    p_idx += 1
                elif child.tag == W + "tbl":
                    # Compound id keeps nested-table seg_ids distinct from the cell's own paragraphs and from other nested tables.
                    nested_id = f"{body_p_idx}.nt{row_idx}.{cell_idx}"
                    segments.extend(extract_table(
                        child, part=part, body_p_idx=nested_id,
                        root=root, parent_map=parent_map,
                    ))
                elif child.tag == W + "sdt":
                    # A content control wrapping the cell's paragraphs (form templates).
                    segments.extend(extract_sdt(
                        child, part=part, body_p_idx=body_p_idx,
                        root=root, parent_map=parent_map,
                    ))
    return segments


def extract_sdt(sdt: ET.Element, *, part: str, body_p_idx: int,
                root: ET.Element, parent_map: dict) -> list[dict]:
    """Extract a content control, treating <w:sdtContent> as a transparent container.

    :param sdt: <w:sdt> element.
    :param part: XML part name.
    :param body_p_idx: index of the top-level body child hosting the control.
    :param root: root of the part's tree.
    :param parent_map: result of build_parent_map for that tree.
    :return: segment dicts; empty when the control has no content.
    """
    segments = []
    sdt_content = sdt.find(W + "sdtContent")
    if sdt_content is None:
        return segments
    p_counter = 0
    for child in sdt_content:
        if child.tag == W + "p":
            segments.extend(extract_paragraph_direct(
                child, part=part, body_p_idx=body_p_idx,
                location_type="sdt", root=root, parent_map=parent_map,
                para_idx_in_container=p_counter,
            ))
            segments.extend(extract_drawings_in(
                child, part=part, body_p_idx=body_p_idx,
                root=root, parent_map=parent_map,
            ))
            p_counter += 1
        elif child.tag == W + "tbl":
            segments.extend(extract_table(
                child, part=part, body_p_idx=body_p_idx,
                root=root, parent_map=parent_map,
            ))
        elif child.tag == W + "sdt":
            segments.extend(extract_sdt(
                child, part=part, body_p_idx=body_p_idx,
                root=root, parent_map=parent_map,
            ))
    return segments


# ---------------------------------------------------------------------------
# Per-part driver
# ---------------------------------------------------------------------------

def extract_part(xml_bytes: bytes, part: str) -> list[dict]:
    """Extract all segments of one XML part.

    :param xml_bytes: the part's XML.
    :param part: part name in the archive.
    :return: segment dicts in document order, with unique seg_ids.
    """
    root = ET.fromstring(xml_bytes)
    parent_map = build_parent_map(root)

    # Headers, footers, footnotes and endnotes have no <w:body> wrapper.
    body = root.find(W + "body")
    if body is None:
        body = root

    segments = []
    for body_idx, child in enumerate(body):
        tag = child.tag
        if tag == W + "p":
            segments.extend(extract_paragraph_direct(
                child, part=part, body_p_idx=body_idx,
                location_type="body_direct", root=root, parent_map=parent_map,
                para_idx_in_container=0,
            ))
            segments.extend(extract_drawings_in(
                child, part=part, body_p_idx=body_idx,
                root=root, parent_map=parent_map,
            ))
        elif tag == W + "tbl":
            segments.extend(extract_table(
                child, part=part, body_p_idx=body_idx,
                root=root, parent_map=parent_map,
            ))
        elif tag == W + "sdt":
            segments.extend(extract_sdt(
                child, part=part, body_p_idx=body_idx,
                root=root, parent_map=parent_map,
            ))
        elif tag in NOTE_WRAPPER_TAGS:
            note_p_idx = 0
            for sub in child:
                if sub.tag == W + "p":
                    segments.extend(extract_paragraph_direct(
                        sub, part=part, body_p_idx=body_idx,
                        location_type="body_direct", root=root, parent_map=parent_map,
                        para_idx_in_container=note_p_idx,
                    ))
                    segments.extend(extract_drawings_in(
                        sub, part=part, body_p_idx=body_idx,
                        root=root, parent_map=parent_map,
                    ))
                    note_p_idx += 1
                elif sub.tag == W + "tbl":
                    segments.extend(extract_table(
                        sub, part=part, body_p_idx=body_idx,
                        root=root, parent_map=parent_map,
                    ))
    return _make_seg_ids_unique(segments)


def _make_seg_ids_unique(segments: list[dict]) -> list[dict]:
    """Give colliding seg_ids a "~N" suffix from their second occurrence on.

    :param segments: segment dicts; mutated in place.
    :return: the same list.
    """
    seen: Counter = Counter()
    for seg in segments:
        seen[seg["seg_id"]] += 1
        if seen[seg["seg_id"]] > 1:
            seg["seg_id"] = f"{seg['seg_id']}~{seen[seg['seg_id']]}"
    return segments


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(src: str, out: str, _job_dir: Path | None = None) -> None:
    """Extract a .docx into a JSON file of segments.

    :param src: path of the input .docx.
    :param out: path of the output JSON.
    :param _job_dir: job directory whose manifest status is set to "extracted"; None skips the manifest update.
    """
    src_path = Path(src)
    out_path = Path(out)

    with zipfile.ZipFile(src_path) as z:
        text_parts = discover_text_parts(z.namelist())
        all_segments = []
        for part in text_parts:
            xml_bytes = z.read(part)
            all_segments.extend(extract_part(xml_bytes, part))

    payload = {
        "source_file": str(src_path),
        "parts": text_parts,
        "segment_count": len(all_segments),
        "segments": all_segments,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    by_loc: Counter = Counter(s["location_type"] for s in all_segments)
    by_part: Counter = Counter(s["part"] for s in all_segments)
    logger.info("Extracted %d segments from %d parts -> %s", len(all_segments), len(text_parts), out_path)
    logger.info("By location: %s", dict(by_loc))
    logger.info("By part: %s", dict(by_part))

    if _job_dir:
        from job_utils import update_manifest
        update_manifest(_job_dir, status="extracted")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from job_utils import parse_job_flag, JobPaths
    job_dir, remaining = parse_job_flag(sys.argv[1:])
    if job_dir:
        jp = JobPaths(job_dir)
        main(str(jp.source_docx), str(jp.extract_json), _job_dir=job_dir)
    else:
        if len(sys.argv) != 3:
            logger.error("Usage: python extract.py <input.docx> <output.json>")
            sys.exit(1)
        main(sys.argv[1], sys.argv[2])
