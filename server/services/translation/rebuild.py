"""
rebuild.py - Reconstruct a translated .docx from the original .docx + a
translation.json (or, with --roundtrip, no translation - just read+write
to verify XML round-trip fidelity).

Strategy:
  1. Open the source .docx as a ZipFile.
  2. For each text-bearing XML part:
     a. Pre-register every namespace prefix declared on the part's root
        element (extracted via regex from the raw bytes), so ElementTree
        does NOT mangle prefixes (turning w:p into ns0:p, etc.).
     b. Parse the XML.
     c. Apply translations (or no-op for round-trip).
     d. Serialize back to bytes, preserving the XML declaration.
  3. Write a new .docx by copying every non-modified zip entry byte-for-byte
     and substituting the modified XML parts.

Translation redistribution strategies (per segment):
  - uniform                   - all runs share fmt_hash; put translated text
                                in run[0], blank the rest.
  - bilingual_format_split    - paragraph contains both languages; only
                                replace the runs whose fmt_hash matches the
                                primary-language signature.
  - slash                     - "X / Y" - replace only the X portion.
  - proportional              - mixed formatting; distribute proportionally,
                                using DNT tokens as anchors when possible.

Usage:
    python rebuild.py --roundtrip <input.docx> <output.docx>
    python rebuild.py <input.docx> <translation.json> <output.docx>
"""

from __future__ import annotations
import json
import logging
import re
import sys
import zipfile
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
W = f"{{{W_NS}}}"
MC = f"{{{MC_NS}}}"


# ---------------------------------------------------------------------------
# Namespace pre-registration (avoid ns0:, ns1: prefix mangling)
# ---------------------------------------------------------------------------

XMLNS_DECL_RE = re.compile(rb'xmlns:([A-Za-z0-9_]+)="([^"]+)"')
XMLNS_DEFAULT_RE = re.compile(rb'xmlns="([^"]+)"')
ROOT_TAG_RE = re.compile(rb'<(?!\?)\S[^>]*>', re.DOTALL)  # first element after decl


# Conventional Word/OOXML prefixes, registered so ElementTree never invents ns10-style
# placeholders, even for namespaces declared deep in the document.
_OOXML_NS_PRESET = {
    "w":      "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "mc":     "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "r":      "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "m":      "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "v":      "urn:schemas-microsoft-com:vml",
    "o":      "urn:schemas-microsoft-com:office:office",
    "w10":    "urn:schemas-microsoft-com:office:word",
    "w14":    "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15":    "http://schemas.microsoft.com/office/word/2012/wordml",
    "w16":    "http://schemas.microsoft.com/office/word/2018/wordml",
    "w16cid": "http://schemas.microsoft.com/office/word/2016/wordml/cid",
    "w16se":  "http://schemas.microsoft.com/office/word/2015/wordml/symex",
    "w16du":  "http://schemas.microsoft.com/office/word/2023/wordml/word16du",
    "w16sdtdh":"http://schemas.microsoft.com/office/word/2020/wordml/sdtdatahash",
    "wp":     "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "wp14":   "http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing",
    "wpc":    "http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas",
    "wpg":    "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup",
    "wps":    "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    "wpi":    "http://schemas.microsoft.com/office/word/2010/wordprocessingInk",
    "a":      "http://schemas.openxmlformats.org/drawingml/2006/main",
    "a14":    "http://schemas.microsoft.com/office/drawing/2010/main",
    "a15":    "http://schemas.microsoft.com/office/drawing/2012/main",
    "a16":    "http://schemas.microsoft.com/office/drawing/2014/main",
    "pic":    "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "dgm":    "http://schemas.openxmlformats.org/drawingml/2006/diagram",
    "c":      "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "cx":     "http://schemas.microsoft.com/office/drawing/2014/chartex",
    "cx1":    "http://schemas.microsoft.com/office/drawing/2015/9/8/chartex",
    "cx2":    "http://schemas.microsoft.com/office/drawing/2015/10/21/chartex",
    "cx3":    "http://schemas.microsoft.com/office/drawing/2016/5/9/chartex",
    "cx4":    "http://schemas.microsoft.com/office/drawing/2016/5/10/chartex",
    "cx5":    "http://schemas.microsoft.com/office/drawing/2016/5/11/chartex",
    "cx6":    "http://schemas.microsoft.com/office/drawing/2016/5/12/chartex",
    "cx7":    "http://schemas.microsoft.com/office/drawing/2016/5/13/chartex",
    "cx8":    "http://schemas.microsoft.com/office/drawing/2016/5/14/chartex",
    "aink":   "http://schemas.microsoft.com/office/drawing/2016/ink",
    "am3d":   "http://schemas.microsoft.com/office/drawing/2017/model3d",
    "xml":    "http://www.w3.org/XML/1998/namespace",
}


def extract_original_root_tag(xml_bytes: bytes) -> bytes:
    """Extract the complete opening tag of the root element.

    The tag carries every namespace declaration, even unused ones, and attributes such as mc:Ignorable; it is preserved exactly so Word does not reject undeclared prefixes.

    :param xml_bytes: XML part bytes.
    :return: the root opening tag; empty bytes when none is found.
    """
    search_start = 0
    if xml_bytes.lstrip()[:5] == b"<?xml":
        end_decl = xml_bytes.find(b"?>")
        if end_decl != -1:
            search_start = end_decl + 2
    m = ROOT_TAG_RE.search(xml_bytes, search_start)
    if m:
        return m.group(0)
    return b""


def merge_root_tags(new_xml: bytes, orig_root_tag: bytes) -> bytes:
    """Replace the root opening tag of re-serialized XML with the original tag extended by the namespace declarations only the new tag has.

    The new tag may hold declarations hoisted from inner elements (e.g. pic:, a14:).

    :param new_xml: XML bytes serialized by ElementTree.
    :param orig_root_tag: root opening tag of the original XML; empty means no merge.
    :return: the XML bytes with the merged root tag.
    """
    if not orig_root_tag:
        return new_xml
    search_start = 0
    if new_xml.lstrip()[:5] == b"<?xml":
        end_decl = new_xml.find(b"?>")
        if end_decl != -1:
            search_start = end_decl + 2
    m = ROOT_TAG_RE.search(new_xml, search_start)
    if not m:
        return new_xml
    new_root_tag = m.group(0)

    orig_ns = {m2.group(1): m2.group(0)
               for m2 in XMLNS_DECL_RE.finditer(orig_root_tag)}
    new_ns = {m2.group(1): m2.group(0)
              for m2 in XMLNS_DECL_RE.finditer(new_root_tag)}

    extra_decls = []
    for prefix, full_decl in new_ns.items():
        if prefix not in orig_ns:
            extra_decls.append(b" " + full_decl)

    if extra_decls:
        if orig_root_tag.endswith(b"/>"):
            insert_pos = len(orig_root_tag) - 2
            suffix = b"/>"
        else:
            insert_pos = len(orig_root_tag) - 1
            suffix = b">"
        merged = orig_root_tag[:insert_pos] + b"".join(extra_decls) + suffix
    else:
        merged = orig_root_tag

    return new_xml[:m.start()] + merged + new_xml[m.end():]


def register_namespaces_from_xml(xml_bytes: bytes) -> dict[str, str]:
    """Register every conventional OOXML prefix and any other xmlns declaration of the XML with ElementTree, so serialization never invents ns10-style prefixes.

    :param xml_bytes: XML part bytes; inner declarations and the default namespace (within the first 8 KiB) are included.
    :return: merged {prefix: uri} mapping; the default namespace has the empty prefix.
    """
    ns_map = dict(_OOXML_NS_PRESET)
    for prefix, uri in _OOXML_NS_PRESET.items():
        ET.register_namespace(prefix, uri)
    for m in XMLNS_DECL_RE.finditer(xml_bytes):
        prefix = m.group(1).decode("ascii")
        uri = m.group(2).decode("utf-8")
        if ns_map.get(prefix) == uri:
            continue
        ns_map[prefix] = uri
        ET.register_namespace(prefix, uri)
    m = XMLNS_DEFAULT_RE.search(xml_bytes[:8192])
    if m:
        uri = m.group(1).decode("utf-8")
        ns_map[""] = uri
        ET.register_namespace("", uri)
    return ns_map


# ---------------------------------------------------------------------------
# Path navigation
# ---------------------------------------------------------------------------

def walk_path(root: ET.Element, path: list) -> ET.Element | None:
    """Follow a positional path from the root.

    :param root: root element.
    :param path: [[tag, child index], ...] as produced by extract.py.
    :return: the target element, or None when the path does not match the tree.
    """
    cur = root
    for tag, idx in path:
        children = list(cur)
        if idx >= len(children):
            return None
        if children[idx].tag != tag:
            return None
        cur = children[idx]
    return cur


# ---------------------------------------------------------------------------
# Text replacement strategies
# ---------------------------------------------------------------------------

def get_runs(p: ET.Element) -> list[ET.Element]:
    """List the <w:r> elements under a paragraph in document order, without descending into drawings and text boxes (separate segments).

    :param p: <w:p> element.
    :return: run elements.
    """
    out = []
    SKIP = {W + "drawing", MC + "AlternateContent", W + "pict"}

    def walk(elem: ET.Element) -> None:
        """Collect the runs below an element into the enclosing list.

        :param elem: element whose descendants are visited.
        """
        for child in elem:
            if child.tag in SKIP:
                continue
            if child.tag == W + "r":
                out.append(child)
            walk(child)
    walk(p)
    return out


def get_t_elements(run: ET.Element) -> list[ET.Element]:
    """List the <w:t> elements directly inside a run, in order.

    :param run: <w:r> element.
    :return: text elements.
    """
    return [t for t in run if t.tag == W + "t"]


def _set_t(t: ET.Element, text: str) -> None:
    """Set the text of a <w:t> element, adding xml:space="preserve" when the text has outer whitespace.

    :param t: <w:t> element.
    :param text: new text.
    """
    t.text = text
    if text != text.strip():
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _new_t(ref: ET.Element, text: str) -> ET.Element:
    """Create a <w:t> element with the given text.

    :param ref: element whose factory builds the new element.
    :param text: text of the new element.
    :return: the new element.
    """
    t = ref.makeelement(W + "t", {})
    _set_t(t, text)
    return t


_SEP_TAGS = {W + "tab": "\t", W + "br": "\n"}

# Not allowed in XML 1.0; lxml refuses them.
_XML_ILLEGAL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _write_runs_text(runs: list, new_text: str) -> bool:
    """Write a text into runs, turning its tab and newline characters into the runs' own <w:tab/> and <w:br/>.

    Control characters illegal in XML 1.0 are stripped. When the text has the same separators in the same order as the runs, each piece goes into the slot between the original separators; otherwise the sequence is rebuilt in the first text run, reusing the original separators of each kind.

    :param runs: <w:r> elements to write into.
    :param new_text: text to write.
    :return: False when the runs hold no <w:t> element, otherwise True.
    """
    new_text = _XML_ILLEGAL_RE.sub("", new_text)
    items = []  # (run, elem, kind) in document order; kind 't', '\t' or '\n'
    for r in runs:
        for c in list(r):
            if c.tag == W + "noBreakHyphen":
                # extract.py reads it as U+2011 inside the text being rewritten.
                r.remove(c)
            elif c.tag == W + "t":
                items.append((r, c, "t"))
            elif c.tag in _SEP_TAGS:
                items.append((r, c, _SEP_TAGS[c.tag]))
    if not any(k == "t" for _, _, k in items):
        return False
    parts = re.split(r"([\t\n])", new_text)
    texts, new_seps = parts[0::2], parts[1::2]
    old_seps = [(r, c, k) for r, c, k in items if k != "t"]

    if [k for _, _, k in old_seps] == new_seps:
        slots: list[list] = [[] for _ in texts]
        i = 0
        for _, c, k in items:
            if k == "t":
                slots[i].append(c)
            else:
                i += 1
        for i, text in enumerate(texts):
            if slots[i]:
                _set_t(slots[i][0], text)
                for t in slots[i][1:]:
                    t.text = ""
            elif text:
                if i < len(old_seps):
                    r, sep, _ = old_seps[i]
                    r.insert(list(r).index(sep), _new_t(sep, text))
                else:
                    r, sep, _ = old_seps[i - 1]
                    r.insert(list(r).index(sep) + 1, _new_t(sep, text))
        return True

    # The LLM merged or split lines: reusing original separators keeps a page break's w:type.
    first_r, first_t = next((r, c) for r, c, k in items if k == "t")
    pools = {"\t": [(r, c) for r, c, k in old_seps if k == "\t"],
             "\n": [(r, c) for r, c, k in old_seps if k == "\n"]}
    for r, c, k in items:
        if k == "t" and c is not first_t:
            r.remove(c)
    _set_t(first_t, texts[0])
    anchor = first_t
    for sep, text in zip(new_seps, texts[1:]):
        if pools[sep]:
            r, el = pools[sep].pop(0)
            r.remove(el)
        else:
            el = first_t.makeelement(W + ("tab" if sep == "\t" else "br"), {})
        new = [el, _new_t(first_t, text)] if text else [el]
        for e in new:
            first_r.insert(list(first_r).index(anchor) + 1, e)
            anchor = e
    return True


def replace_uniform(p: ET.Element, new_text: str) -> bool:
    """Write the whole text over all runs of a paragraph; used when the runs share one formatting.

    :param p: <w:p> element.
    :param new_text: replacement text.
    :return: True when the text was written.
    """
    runs = get_runs(p)
    if not runs:
        return False
    return _write_runs_text(runs, new_text)


def replace_bilingual_format_split(p: ET.Element, new_text: str,
                                   primary_fmt: str) -> bool:
    """Replace only the runs whose format hash matches the primary side of a bilingual paragraph; the kept-language runs stay untouched.

    :param p: <w:p> element.
    :param new_text: translated text of the primary side only.
    :param primary_fmt: format hash of the runs to replace.
    :return: True when the text was written, False when no run matches.
    """
    runs = get_runs(p)
    if not runs:
        return False
    matching = [r for r in runs if _fmt_hash(r.find(W + "rPr")) == primary_fmt]
    return _write_runs_text(matching, new_text)


def replace_slash_split(p: ET.Element, new_left: str,
                        original_left_text: str) -> bool:
    """Replace the part before the "/" of an "X / Y" paragraph; the separator and the right half are preserved.

    :param p: <w:p> element.
    :param new_left: replacement for the left half.
    :param original_left_text: original left half (unused).
    :return: True when the text was written, False when the paragraph has no text or no "/".
    """
    runs = get_runs(p)
    all_ts = []
    for r in runs:
        all_ts.extend(get_t_elements(r))
    if not all_ts:
        return False
    full = "".join(t.text or "" for t in all_ts)
    if "/" not in full:
        return False
    slash_idx = full.index("/")
    after_slash = full[slash_idx:]
    sep_prefix = " " if (slash_idx > 0 and full[slash_idx - 1] == " ") else ""
    new_full = new_left.rstrip() + sep_prefix + after_slash
    return _redistribute_across_ts(all_ts, new_full)


def replace_proportional(p: ET.Element, new_text: str) -> bool:
    """Distribute a text across all <w:t> elements proportionally to their original lengths; approximate, format boundaries may shift mid-word.

    :param p: <w:p> element.
    :param new_text: replacement text.
    :return: True when the text was written.
    """
    runs = get_runs(p)
    all_ts = []
    for r in runs:
        all_ts.extend(get_t_elements(r))
    if not all_ts:
        return False
    return _redistribute_across_ts(all_ts, new_text)


def _redistribute_across_ts(ts: list[ET.Element], new_text: str) -> bool:
    """Distribute a text across <w:t> elements proportionally to their original lengths; the last one takes the remainder.

    :param ts: <w:t> elements.
    :param new_text: text to distribute.
    :return: False when ts is empty, otherwise True.
    """
    if not ts:
        return False
    if len(ts) == 1:
        ts[0].text = new_text
        if new_text != new_text.strip():
            ts[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        return True
    orig_lengths = [len(t.text or "") for t in ts]
    total_orig = sum(orig_lengths) or 1
    n = len(new_text)
    cursor = 0
    for i, t in enumerate(ts):
        if i == len(ts) - 1:
            chunk = new_text[cursor:]
        else:
            take = round(n * orig_lengths[i] / total_orig)
            chunk = new_text[cursor:cursor + take]
            cursor += len(chunk)
        t.text = chunk
        if chunk != chunk.strip():
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return True


def _fmt_hash(rPr: ET.Element | None) -> str:
    """Hash the formatting properties of a run; identical to extract.fmt_hash and must stay in sync with it.

    :param rPr: the run's <w:rPr> element; None for an unformatted run.
    :return: the fingerprint; "" when rPr is None.
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


# ---------------------------------------------------------------------------
# Apply translations to a parsed XML tree
# ---------------------------------------------------------------------------

def apply_translations(root: ET.Element, segments_for_part: list[dict],
                       choice_paths: dict, fallback_paths: dict,
                       fmt_signatures: dict, inline_splits: dict,
                       pattern_types: dict) -> int:
    """Apply translations to one parsed ElementTree XML part.

    Both the mc:Choice and the mc:Fallback copy of a paragraph are updated identically.

    :param root: root element of the part.
    :param segments_for_part: translation segments of this part.
    :param choice_paths: seg_id to positional path of the paragraph.
    :param fallback_paths: seg_id to positional path of the mc:Fallback twin; None entries are skipped.
    :param fmt_signatures: seg_id to dominant format hash.
    :param inline_splits: seg_id to inline split info; None when the segment has none.
    :param pattern_types: seg_id to pattern type; missing means "mono".
    :return: number of segments processed, excluding those marked keep_as_is.
    """
    applied = 0
    for seg in segments_for_part:
        seg_id = seg["seg_id"]
        if seg.get("keep_as_is"):
            continue
        new_text = seg["translated_text"]
        ch_path = choice_paths.get(seg_id)
        fb_path = fallback_paths.get(seg_id)
        fmt_sig = fmt_signatures.get(seg_id, "")
        inline_split = inline_splits.get(seg_id)
        pattern = pattern_types.get(seg_id, "mono")

        for path in (ch_path, fb_path):
            if not path:
                continue
            target = walk_path(root, path)
            if target is None:
                continue
            if pattern == "bilingual_inline_slash" and inline_split:
                replace_slash_split(target, new_text, seg["original_text"])
            elif pattern == "bilingual_inline_concat" and inline_split:
                primary_fmt = inline_split.get("fmt_a", fmt_sig)
                replace_bilingual_format_split(target, new_text, primary_fmt)
            else:
                replace_uniform(target, new_text)
        applied += 1
    return applied


# ---------------------------------------------------------------------------
# lxml-based translation application
# ---------------------------------------------------------------------------
# lxml elements share the ET API, so path-walking and run replacement are reused.

def apply_translations_lxml(root: Any, segments_for_part: list[dict], choice_paths: dict,
                            fallback_paths: dict, fmt_signatures: dict, inline_splits: dict,
                            pattern_types: dict) -> int:
    """Apply translations to one parsed lxml XML part.

    A format-split bilingual segment (span_translated) carries only the source-side span and is spliced into the runs of that side; no fallback to uniform replacement, so an unmatched format leaves the paragraph untouched. Every other segment carries the full replacement text.

    :param root: root element of the part.
    :param segments_for_part: translation segments of this part.
    :param choice_paths: seg_id to positional path of the paragraph.
    :param fallback_paths: seg_id to positional path of the mc:Fallback twin; None entries are skipped.
    :param fmt_signatures: seg_id to dominant format hash.
    :param inline_splits: seg_id to inline split info; None when the segment has none.
    :param pattern_types: seg_id to pattern type (unused here).
    :return: number of segments processed, excluding those marked keep_as_is.
    """
    applied = 0
    for seg in segments_for_part:
        seg_id = seg["seg_id"]
        if seg.get("keep_as_is"):
            continue
        new_text = seg["translated_text"]
        ch_path = choice_paths.get(seg_id)
        fb_path = fallback_paths.get(seg_id)
        fmt_sig = fmt_signatures.get(seg_id, "")
        inline_split = inline_splits.get(seg_id)
        pattern = pattern_types.get(seg_id, "mono")

        for path in (ch_path, fb_path):
            if not path:
                continue
            target = walk_path(root, path)
            if target is None:
                continue
            if inline_split and inline_split.get("span_translated"):
                # Uniform replacement would wipe the kept-language side.
                replace_bilingual_format_split(
                    target, new_text, inline_split.get("source_fmt") or fmt_sig)
            else:
                replace_uniform(target, new_text)
        applied += 1
    return applied


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def rebuild_docx(src_docx: str, out_docx: str,
                 translations_by_part: dict[str, list[dict]] | None = None,
                 metadata: dict | None = None) -> None:
    """Write a translated .docx from a source .docx and translations; every unmodified zip entry is copied byte-for-byte.

    :param src_docx: path of the source .docx.
    :param out_docx: path of the output .docx; parent directories are created.
    :param translations_by_part: part name to translation segments; None or empty means a pure round-trip that re-serializes every text-bearing part.
    :param metadata: choice_paths, fallback_paths, fmt_signatures, inline_splits and pattern_types, as built by load_translation_with_metadata.
    :raises RuntimeError: when lxml is not installed.
    """
    src = Path(src_docx)
    out = Path(out_docx)
    out.parent.mkdir(parents=True, exist_ok=True)
    metadata = metadata or {}

    with zipfile.ZipFile(src, "r") as zin:
        names = zin.namelist()
        new_bytes: dict[str, bytes] = {}
        modified_parts = (set(translations_by_part.keys())
                          if translations_by_part else set())
        # A round-trip re-serializes every text-bearing part to test fidelity.
        if not translations_by_part:
            modified_parts = {n for n in names
                              if n.startswith("word/") and n.endswith(".xml")
                              and (n.endswith("/document.xml")
                                   or "header" in n or "footer" in n
                                   or n.endswith("footnotes.xml")
                                   or n.endswith("endnotes.xml"))}

        for part in modified_parts:
            if part not in names:
                continue
            xml_bytes = zin.read(part)

            # lxml keeps every namespace declaration, attribute order and xml:space, unlike
            # ElementTree; Word rejects files whose mc:Ignorable names undeclared prefixes.
            try:
                from lxml import etree as lxml_etree
                parser = lxml_etree.XMLParser(
                    remove_blank_text=False,
                    strip_cdata=False,
                )
                lxml_root = lxml_etree.fromstring(xml_bytes, parser)
            except ImportError:
                raise RuntimeError(
                    "lxml is required for reliable .docx round-tripping. "
                    "Install with: pip install lxml"
                )

            if translations_by_part and part in translations_by_part:
                segs = translations_by_part[part]
                ch_paths = metadata.get("choice_paths", {})
                fb_paths = metadata.get("fallback_paths", {})
                fmt_sigs = metadata.get("fmt_signatures", {})
                inline_splits = metadata.get("inline_splits", {})
                pattern_types = metadata.get("pattern_types", {})
                applied = apply_translations_lxml(
                    lxml_root, segs, ch_paths, fb_paths, fmt_sigs,
                    inline_splits, pattern_types,
                )
                logger.info("%s: applied %d/%d translations", part, applied, len(segs))

            raw = lxml_etree.tostring(
                lxml_root,
                xml_declaration=True,
                encoding="UTF-8",
                standalone=True,
            )
            # Match Word's XML form: double quotes in the declaration, CRLF after it.
            raw = raw.replace(
                b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>",
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                1,
            )
            raw = raw.replace(b'?>\n<', b'?>\r\n<', 1)
            new_bytes[part] = raw

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                if name in new_bytes:
                    zout.writestr(name, new_bytes[name])
                else:
                    zout.writestr(name, zin.read(name))

    logger.info("Wrote %s", out)


# ---------------------------------------------------------------------------
# Translation-file ingestion
# ---------------------------------------------------------------------------

def load_translation_with_metadata(translation_path: str,
                                   extract_path: str,
                                   audit_path: str) -> tuple[dict, dict]:
    """Load translation.json and cross-reference extract.json and audit.json.

    :param translation_path: path of translation.json.
    :param extract_path: path of extract.json.
    :param audit_path: path of audit.json.
    :return: (translation segments by part name, metadata dict with per-segment xml paths, format signatures, inline splits and pattern types).
    """
    tr = json.loads(Path(translation_path).read_text(encoding="utf-8"))
    ex = json.loads(Path(extract_path).read_text(encoding="utf-8"))
    au = json.loads(Path(audit_path).read_text(encoding="utf-8"))

    by_id_extract = {s["seg_id"]: s for s in ex["segments"]}
    by_id_audit = {s["seg_id"]: s for s in au["segments"]}

    metadata = {
        "choice_paths": {},
        "fallback_paths": {},
        "fmt_signatures": {},
        "inline_splits": {},
        "pattern_types": {},
    }
    translations_by_part: dict[str, list[dict]] = defaultdict(list)
    for tseg in tr["segments"]:
        seg_id = tseg["seg_id"]
        ext = by_id_extract.get(seg_id)
        aud = by_id_audit.get(seg_id)
        if not ext:
            continue
        translations_by_part[ext["part"]].append(tseg)
        metadata["choice_paths"][seg_id] = ext["xml_choice_path"]
        metadata["fallback_paths"][seg_id] = ext.get("xml_fallback_path")
        metadata["fmt_signatures"][seg_id] = ext["fmt_signature"]
        if aud:
            metadata["pattern_types"][seg_id] = aud["pattern_type"]
            metadata["inline_splits"][seg_id] = aud.get("inline_split")
    return translations_by_part, metadata


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def usage() -> None:
    """Log the usage line of the module docstring."""
    logger.info(__doc__.strip().splitlines()[-3].strip())


def main(argv: list[str]) -> None:
    """Command-line entry point: rebuild from a job directory, round-trip a file, or rebuild from explicit paths.

    :param argv: command-line arguments, program name first: --job <dir>, or --roundtrip <input.docx> <output.docx>, or <input.docx> <translation.json> <output.docx>.
    """
    from job_utils import parse_job_flag, JobPaths, update_manifest
    job_dir, remaining = parse_job_flag(argv[1:])
    if job_dir:
        jp = JobPaths(job_dir)
        translations_by_part, metadata = load_translation_with_metadata(
            str(jp.translation_json), str(jp.extract_json), str(jp.audit_json),
        )
        rebuild_docx(str(jp.source_docx), str(jp.output_docx),
                      translations_by_part, metadata)
        update_manifest(job_dir, status="rebuilt")
        return

    if len(argv) >= 2 and argv[1] == "--roundtrip":
        if len(argv) != 4:
            logger.error("Usage: python rebuild.py --roundtrip <input.docx> <output.docx>")
            sys.exit(1)
        rebuild_docx(argv[2], argv[3])
        return
    if len(argv) != 4:
        logger.error("Usage: python rebuild.py <input.docx> <translation.json> <output.docx>")
        logger.error("       python rebuild.py --roundtrip <input.docx> <output.docx>")
        sys.exit(1)
    src_docx, translation_json, out_docx = argv[1], argv[2], argv[3]
    # <stem>.translation.json pairs with <stem>.extract.json and <stem>.audit.json.
    tr_path = Path(translation_json)
    stem = tr_path.stem.replace(".translation", "")
    reports_dir = tr_path.parent
    extract_path = reports_dir / f"{stem}.extract.json"
    audit_path = reports_dir / f"{stem}.audit.json"
    translations_by_part, metadata = load_translation_with_metadata(
        translation_json, str(extract_path), str(audit_path),
    )
    rebuild_docx(src_docx, out_docx, translations_by_part, metadata)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main(sys.argv)
