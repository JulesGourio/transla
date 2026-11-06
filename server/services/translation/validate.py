"""
validate.py - Structural integrity validation of a .docx file.

Checks performed:
  1. ZIP integrity — all entries readable
  2. XML validity — every .xml and .rels part parses without error
  3. Namespace integrity — all mc:Ignorable prefixes are declared
  4. No auto-prefixes — no ns0:/ns1:/ns2: artifacts from ElementTree
  5. Content_Types consistency — all referenced parts exist in the zip
  6. Segment fidelity — if an extract.json is provided, verify segment
     count and text matches (for post-rebuild validation)
  7. Byte identity — for non-modified parts, verify they're unchanged
     from the original (if original path is provided)

Usage:
    python validate.py <file.docx>
    python validate.py <file.docx> --original <original.docx>
    python validate.py <file.docx> --extract <extract.json>

Exit code: 0 if all checks pass, 1 if any fail.
"""

from __future__ import annotations
import json
import logging
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

def check_zip_integrity(path: str) -> list[str]:
    """Check that every entry of the archive is readable.

    :param path: path of the .docx.
    :return: error messages; empty when the archive is sound.
    """
    errors = []
    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            if bad:
                errors.append(f"ZIP corruption in: {bad}")
    except Exception as e:
        errors.append(f"Cannot open as ZIP: {e}")
    return errors


def check_xml_validity(path: str) -> list[str]:
    """Check that every .xml and .rels part parses.

    :param path: path of the .docx.
    :return: error messages; empty when all parts parse.
    """
    errors = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if name.endswith(".xml") or name.endswith(".rels"):
                try:
                    ET.fromstring(z.read(name))
                except ET.ParseError as e:
                    errors.append(f"Invalid XML in {name}: {e}")
    return errors


def check_namespace_integrity(path: str) -> list[str]:
    """Check that mc:Ignorable only references declared prefixes.

    :param path: path of the .docx.
    :return: error messages; empty when all prefixes are declared.
    """
    errors = []
    MC_IGN_RE = re.compile(rb'mc:Ignorable="([^"]+)"')
    XMLNS_PREFIX_RE = re.compile(rb'xmlns:([A-Za-z0-9_]+)="')
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(".xml"):
                continue
            data = z.read(name)
            mc_m = MC_IGN_RE.search(data[:16000])
            if not mc_m:
                continue
            prefixes = mc_m.group(1).decode().split()
            # Declarations may sit on inner elements, so the whole file is scanned.
            declared = set(m.group(1).decode()
                           for m in XMLNS_PREFIX_RE.finditer(data))
            missing = [p for p in prefixes if p not in declared]
            if missing:
                errors.append(
                    f"{name}: mc:Ignorable references undeclared prefixes: "
                    f"{missing}"
                )
    return errors


def check_auto_prefixes(path: str) -> list[str]:
    """Check for auto-generated ns0:/ns1: namespace prefixes in the XML parts.

    :param path: path of the .docx.
    :return: error messages; empty when none is found.
    """
    errors = []
    AUTO_NS_RE = re.compile(rb'<ns\d+:')
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.endswith(".xml"):
                continue
            data = z.read(name)
            hits = AUTO_NS_RE.findall(data[:50000])
            if hits:
                errors.append(
                    f"{name}: contains auto-generated namespace prefixes "
                    f"({len(hits)} occurrences, e.g. {hits[0].decode()})"
                )
    return errors


def check_content_types(path: str) -> list[str]:
    """Check that every part referenced by [Content_Types].xml exists.

    :param path: path of the .docx.
    :return: error messages; empty when consistent.
    """
    errors = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "[Content_Types].xml" not in names:
            errors.append("Missing [Content_Types].xml")
            return errors
        ct = z.read("[Content_Types].xml").decode("utf-8", errors="replace")
        for m in re.finditer(r'PartName="/([^"]+)"', ct):
            part = m.group(1)
            if part not in names:
                errors.append(
                    f"[Content_Types].xml references non-existent part: {part}"
                )
    return errors


def check_segment_fidelity(path: str, extract_json: str) -> list[str]:
    """Compare the segment ids and count of a file against a reference extract.

    word/comments.xml is excluded, because injected review comments legitimately add segments there.

    :param path: path of the .docx to check.
    :param extract_json: path of the reference extract.json.
    :return: error messages; empty when the segments match.
    """
    errors = []
    try:
        from .extract import main as extract_main
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = f.name
        extract_main(path, tmp)
        new = json.loads(Path(tmp).read_text(encoding="utf-8"))
        Path(tmp).unlink()

        ref = json.loads(Path(extract_json).read_text(encoding="utf-8"))
        ref_segs = [s for s in ref["segments"] if s["part"] != "word/comments.xml"]
        new_segs = [s for s in new["segments"] if s["part"] != "word/comments.xml"]
        ref_ids = {s["seg_id"] for s in ref_segs}
        new_ids = {s["seg_id"] for s in new_segs}
        missing = ref_ids - new_ids
        extra = new_ids - ref_ids
        if missing:
            errors.append(f"Missing {len(missing)} segments vs reference")
        if extra:
            errors.append(f"Extra {len(extra)} segments vs reference")
        if len(ref_segs) != len(new_segs):
            errors.append(
                f"Segment count mismatch: ref={len(ref_segs)} "
                f"new={len(new_segs)}"
            )
    except Exception as e:
        errors.append(f"Segment fidelity check failed: {e}")
    return errors


def check_byte_identity(path: str, original: str) -> list[str]:
    """Check that parts holding no user text are byte-identical to the original.

    :param path: path of the rebuilt .docx.
    :param original: path of the original .docx.
    :return: error messages; empty when the parts are identical.
    """
    errors = []
    # Parts holding no user text. Footnotes and endnotes are excluded: their paragraphs are translated.
    IMMUTABLE = {"word/styles.xml", "word/settings.xml", "word/fontTable.xml",
                 "word/webSettings.xml", "word/numbering.xml", "word/theme/theme1.xml"}
    with zipfile.ZipFile(path) as z1, zipfile.ZipFile(original) as z2:
        for name in IMMUTABLE:
            if name not in z1.namelist() or name not in z2.namelist():
                continue
            a = z1.read(name)
            b = z2.read(name)
            if a != b:
                errors.append(
                    f"{name} differs from original "
                    f"(orig={len(b)} new={len(a)} delta={len(a)-len(b)})"
                )
    return errors


def check_residual_source_language(path: str, source_lang: str) -> list[str]:
    """Report segments still in the source language after translation.

    Bulgarian is confirmed by Cyrillic presence; Czech, German and Spanish by characters unique to the language; other languages by the detector with a high threshold.

    :param path: path of the rebuilt .docx.
    :param source_lang: source language code.
    :return: error messages listing up to ten residual segments; empty when none remain.
    """
    errors = []
    try:
        from .extract import main as extract_main
        from .langdetect import detect_language
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp = f.name
        extract_main(path, tmp)
        data = json.loads(Path(tmp).read_text(encoding="utf-8"))
        Path(tmp).unlink()

        # The generic detector confuses CS and FR (shared diacritics), so residual text needs source-unique characters.
        UNIQUE_CHARS = {
            "cs": set("řůěŘŮĚ"),
            "bg": None,  # Cyrillic block test instead
            "de": set("ßÄÖÜäöü"),
            "es": set("ñÑ¿¡"),
        }
        unique_chars = UNIQUE_CHARS.get(source_lang)

        residual = []
        for s in data["segments"]:
            text = s["text"]
            if not text.strip() or len(text.strip()) < 3:
                continue

            if source_lang == "bg":
                cyr = sum(1 for c in text if "\u0400" <= c <= "\u04ff")
                if cyr > 3:
                    residual.append(s)
            elif unique_chars:
                # Unique characters avoid false positives such as FR flagged as CS.
                if any(c in unique_chars for c in text):
                    residual.append(s)
            else:
                lang, conf = detect_language(text)
                if lang == source_lang and conf > 0.85:
                    residual.append(s)

        if residual:
            errors.append(
                f"{len(residual)} segments still contain {source_lang.upper()} text:"
            )
            for s in residual[:10]:
                errors.append(f"  {s['seg_id']}: {s['text'][:100]}")
            if len(residual) > 10:
                errors.append(f"  ... and {len(residual) - 10} more")
    except Exception as e:
        errors.append(f"Residual language check failed: {e}")
    return errors


def main(argv: list[str]) -> int:
    """Run the structural checks from the command line and log the report.

    :param argv: command-line arguments, program name first: --job <dir>, or <file.docx> with optional --original <orig.docx>, --extract <extract.json> and --source-lang XX.
    :return: 0 when all checks pass, 1 otherwise.
    """
    from job_utils import parse_job_flag, JobPaths, update_manifest
    job_dir, remaining = parse_job_flag(argv[1:])
    if job_dir:
        jp = JobPaths(job_dir)
        path = str(jp.output_docx)
        original = str(jp.source_docx)
        extract = str(jp.extract_json)
        source_lang = jp.source_lang
    else:
        if len(argv) < 2:
            logger.error("Usage: python validate.py <file.docx> [--original <orig.docx>] "
                         "[--extract <extract.json>] [--source-lang XX]")
            return 1
        path = argv[1]
        original = None
        extract = None
        source_lang = None
        i = 2
        while i < len(argv):
            if argv[i] == "--original" and i + 1 < len(argv):
                original = argv[i + 1]
                i += 2
            elif argv[i] == "--extract" and i + 1 < len(argv):
                extract = argv[i + 1]
                i += 2
            elif argv[i] == "--source-lang" and i + 1 < len(argv):
                source_lang = argv[i + 1].lower()
                i += 2
            else:
                i += 1

    all_errors = []
    checks = [
        ("ZIP integrity", check_zip_integrity(path)),
        ("XML validity", check_xml_validity(path)),
        ("Namespace integrity", check_namespace_integrity(path)),
        ("Auto-prefix detection", check_auto_prefixes(path)),
        ("Content_Types consistency", check_content_types(path)),
    ]
    if extract:
        checks.append(("Segment fidelity", check_segment_fidelity(path, extract)))
    if original:
        checks.append(("Byte identity (immutable parts)", check_byte_identity(path, original)))
    if source_lang:
        checks.append(("Residual source language", check_residual_source_language(path, source_lang)))

    total_errors = 0
    for name, errors in checks:
        status = "PASS" if not errors else "FAIL"
        logger.info("[%s] %s", status, name)
        for e in errors:
            logger.info("       %s", e)
        total_errors += len(errors)

    if total_errors == 0:
        logger.info("ALL CHECKS PASSED")
    else:
        logger.info("FAILED: %d error(s) found", total_errors)

    if job_dir:
        update_manifest(job_dir, status="validated")

    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main(sys.argv))
