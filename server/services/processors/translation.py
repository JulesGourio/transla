"""Adapter running the translation engine modules in memory against uploaded document bytes."""

import io
import logging
import re
import zipfile
from typing import Any, Dict, List, Tuple

from ..translation import audit as _audit
from ..translation import extract as _extract
from ..translation import fit_check as _fit_check
from ..translation import glossary_io as _glossary_io
from ..translation import langdetect as _langdetect
from ..translation import length_adapt as _length_adapt
from ..translation import rebuild as _rebuild
from ..translation import validate as _validate
from ..translation.docx_images import IMAGE_TRANSLATION_MARKER as _IMAGE_TRANSLATION_MARKER

logger = logging.getLogger(__name__)


def extract_docx_segments(docx_bytes: bytes) -> List[Dict[str, Any]]:
    """Extract the translatable segments of a .docx.

    Text boxes appear in both mc:Choice and mc:Fallback; both positional paths are recorded.

    :param docx_bytes: Document content.
    :return: Segment dicts in the engine's extract format.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
        text_parts = _extract.discover_text_parts(z.namelist())
        segments: List[Dict[str, Any]] = []
        for part in text_parts:
            xml_bytes = z.read(part)
            segments.extend(_extract.extract_part(xml_bytes, part))
    return segments


def audit_segments(
    segments: List[Dict[str, Any]],
    dnt_rows: List[Dict[str, str]],
    source_lang: str | None = None,
    target_lang: str | None = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Detect languages, pair bilingual segments, flag conflicts and generate clarifying questions.

    A DntMatcher is built per call from ``dnt_rows`` so concurrent jobs never share rules; the rules apply on top of the
    built-in DNT_REGEX.

    :param segments: Segments from ``extract_docx_segments``.
    :param dnt_rows: Do-not-translate rules from the ``dnt_rules`` table.
    :param source_lang: Declared source language; with ``target_lang`` it constrains detection and selects the mode.
    :param target_lang: Declared target language.
    :return: ``(audit_report, questions)``; the report holds ``mode``, the detected languages, counts and one merged row per segment.
    """
    matcher = _glossary_io.DntMatcher(dnt_rows)
    audited = _audit.audit_all_segments(segments, matcher, source_lang, target_lang)
    mode = (_audit.determine_mode(audited, source_lang, target_lang)
            if source_lang and target_lang else "bilingual")

    # Pairing only makes sense when two languages sit side by side; on a monolingual document it invents bogus pairs.
    if mode == 'bilingual':
        pairer = _audit.Pairer()
        _audit.pair_txbx_siblings(audited, pairer)
        _audit.pair_adjacent_body(audited, pairer)
        _audit.pair_table_rows(audited, pairer)

    # Inline splits (two languages in one segment) run in both modes: the source span is translated and the other kept
    # even when the declared target never appears elsewhere in the document.
    _audit.pair_inline_slash(audited)
    _audit.pair_inline_format_concat(audited)
    _audit.pair_inline_charspan(audited)

    if mode == 'bilingual':
        pairs_by_id: Dict[str, List[dict]] = {}
        for s in audited:
            if s['pair_id']:
                pairs_by_id.setdefault(s['pair_id'], []).append(s)
        for _pid, segs in pairs_by_id.items():
            if len(segs) != 2:
                continue
            primary = next((s for s in segs if s['pair_role'] == 'primary'), None)
            secondary = next((s for s in segs if s['pair_role'] == 'secondary'), None)
            if primary and secondary:
                _audit.assess_pair(primary, secondary)

    primary_lang, secondary_lang, all_langs = _audit.determine_languages(audited)

    questions = _audit.generate_questions(
        audited, primary_lang, secondary_lang, source_file='', mode=mode,
    )

    # strip_internal keeps only audit fields; rebuild needs part/location/XML paths from the extract to place each translation.
    ext_by_id = {s['seg_id']: s for s in segments}
    merged = []
    for s in audited:
        row = _audit.strip_internal(s)
        ext = ext_by_id.get(row['seg_id'], {})
        for k in ('part', 'location_type', 'xml_choice_path', 'xml_fallback_path'):
            row.setdefault(k, ext.get(k))
        merged.append(row)

    audit_report = {
        'mode': mode,
        'detected_languages': all_langs,
        'primary_language': primary_lang,
        'secondary_language': secondary_lang,
        'segment_count': len(audited),
        'pair_count': len([1 for s in audited if s['pair_role'] == 'primary']),
        'conflict_count': len([1 for s in audited if s['pair_role'] == 'primary' and s['conflict_flag']]),
        'dnt_token_count': sum(len(s['dnt_tokens']) for s in audited),
        'segments': merged,
    }
    return audit_report, questions


# ---------------------------------------------------------------------------
# fit_check / length_adapt — overflow detection + shortening
# ---------------------------------------------------------------------------

def check_fit(
    extract_segments: List[Dict[str, Any]],
    translated_segments: List[Dict[str, Any]],
    threshold_override: int | None = None,
) -> Tuple[List[Dict[str, Any]], int]:
    """Flag segments whose translated text is likely to overflow its container.

    :param extract_segments: Segments from extraction, giving each one's part and location type.
    :param translated_segments: Dicts with ``seg_id``, ``original_text``, ``translated_text``, ``keep_as_is`` and ``inline_split``.
    :param threshold_override: Length ratio in percent replacing the per-location threshold.
    :return: ``(flagged, critical_count)``; flagged items are sorted by severity, then by descending ratio.
    """
    ext_by_id = {s['seg_id']: s for s in extract_segments}
    flagged: List[Dict[str, Any]] = []
    critical = 0
    for tseg in translated_segments:
        if tseg.get('keep_as_is') or tseg.get('translated_text') is None:
            continue
        seg_id = tseg['seg_id']
        ext = ext_by_id.get(seg_id)
        if not ext:
            continue
        orig_len = len(tseg['original_text'])
        new_len = len(tseg['translated_text'])
        if orig_len == 0:
            continue
        inline_split = tseg.get('inline_split')
        if inline_split and inline_split.get('span_translated'):
            # translated_text holds only the source-side span; the rendered length adds the kept side.
            new_len += orig_len - inline_split.get('source_span_len', 0)
        ratio = (new_len / orig_len) * 100
        loc = _fit_check.classify_location(seg_id, ext['location_type'], ext['part'])
        threshold = threshold_override or _fit_check.THRESHOLDS.get(loc, 130)
        severity = _fit_check.SEVERITY.get(loc, 'INFO')
        if ratio > threshold:
            flagged.append({
                'seg_id': seg_id, 'location': loc, 'severity': severity,
                'orig_len': orig_len, 'new_len': new_len, 'ratio': round(ratio, 1),
                'threshold': threshold,
                'original_text': tseg['original_text'][:80],
                'translated_text': tseg['translated_text'][:80],
            })
            if severity == 'CRITICAL':
                critical += 1
    sev_order = {'CRITICAL': 0, 'WARNING': 1, 'INFO': 2}
    flagged.sort(key=lambda f: (sev_order.get(f['severity'], 9), -f['ratio']))
    return flagged, critical


def adapt_lengths(
    extract_segments: List[Dict[str, Any]],
    translated_segments: List[Dict[str, Any]],
    target_lang: str = "fr",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Shorten overflowing translations with the target language's abbreviation rules.

    ``translated_text`` of the given segments is replaced in place.

    :param extract_segments: Segments from extraction, giving each one's part and location type.
    :param translated_segments: Dicts with ``seg_id``, ``original_text``, ``translated_text``, ``keep_as_is`` and ``inline_split``.
    :param target_lang: Target language code selecting the abbreviation rules.
    :return: ``(translated_segments, suggestions)``; each suggestion records the before/after text and whether it now fits.
    """
    ext_by_id = {s['seg_id']: s for s in extract_segments}
    thresholds = {'header': 100, 'footer': 100, 'txbx': 130, 'table': 150}
    suggestions: List[Dict[str, Any]] = []
    for tseg in translated_segments:
        if tseg.get('keep_as_is') or tseg.get('translated_text') is None:
            continue
        inline_split = tseg.get('inline_split')
        if inline_split and inline_split.get('span_translated'):
            # Span-only translation: length targets derived from orig_len would mis-size it.
            continue
        seg_id = tseg['seg_id']
        ext = ext_by_id.get(seg_id)
        if not ext:
            continue
        orig_len = len(tseg['original_text'])
        new_text = tseg['translated_text']
        new_len = len(new_text)
        if orig_len == 0:
            continue
        part = ext['part']
        loc = 'header' if 'header' in part else 'footer' if 'footer' in part else ext['location_type']
        threshold = thresholds.get(loc)
        if threshold is None:
            continue
        ratio = (new_len / orig_len) * 100
        if ratio <= threshold:
            continue

        if loc in ('header', 'footer'):
            alt = _length_adapt.HEADER_ALTERNATIVES_BY_LANG.get(target_lang, {}).get(new_text.strip())
            if alt:
                shortened, rules = alt, ['header alternative']
            else:
                shortened, rules = _length_adapt.shorten(new_text, orig_len, lang=target_lang)
        else:
            shortened, rules = _length_adapt.shorten(new_text, int(orig_len * threshold / 100), lang=target_lang)

        shortened_ratio = (len(shortened) / orig_len) * 100
        suggestions.append({
            'seg_id': seg_id, 'location': loc, 'orig_len': orig_len,
            'before': new_text, 'before_len': new_len,
            'after': shortened, 'after_len': len(shortened),
            'fits': shortened_ratio <= threshold, 'rules': rules,
        })
        tseg['translated_text'] = shortened
    return translated_segments, suggestions


# ---------------------------------------------------------------------------
# rebuild — reconstruct a translated .docx in-memory
# ---------------------------------------------------------------------------

def build_rebuild_inputs(segments: List[Dict[str, Any]]) -> Tuple[Dict[str, List[dict]], Dict[str, Any]]:
    """Group segment rows by XML part and build the per-segment metadata the rebuild expects.

    :param segments: JSON-decoded rows with ``seg_id``, ``part``, ``xml_choice_path``, ``xml_fallback_path``,
        ``pattern_type``, ``inline_split``, ``translated_text``, ``keep_as_is`` and ``original_text``.
    :return: ``(translations_by_part, metadata)``.
    """
    translations_by_part: Dict[str, List[dict]] = {}
    metadata: Dict[str, Any] = {
        'choice_paths': {}, 'fallback_paths': {}, 'fmt_signatures': {},
        'inline_splits': {}, 'pattern_types': {},
    }
    for s in segments:
        part = s['part']
        translations_by_part.setdefault(part, []).append({
            'seg_id': s['seg_id'],
            'original_text': s['original_text'],
            'translated_text': s.get('translated_text'),
            'keep_as_is': bool(s.get('keep_as_is')),
        })
        metadata['choice_paths'][s['seg_id']] = s.get('xml_choice_path')
        metadata['fallback_paths'][s['seg_id']] = s.get('xml_fallback_path')
        metadata['inline_splits'][s['seg_id']] = s.get('inline_split')
        metadata['pattern_types'][s['seg_id']] = s.get('pattern_type') or 'mono'
    return translations_by_part, metadata


def rebuild_docx_bytes(
    src_bytes: bytes,
    translations_by_part: Dict[str, List[dict]],
    metadata: Dict[str, Any],
) -> bytes:
    """Write translations into a copy of the source .docx.

    lxml keeps namespace declarations and attribute formatting byte-for-byte, which Word requires.

    :param src_bytes: Source document content.
    :param translations_by_part: Translations grouped by XML part, from ``build_rebuild_inputs``.
    :param metadata: Per-segment metadata, from ``build_rebuild_inputs``.
    :return: The rebuilt document content.
    """
    from lxml import etree as lxml_etree

    with zipfile.ZipFile(io.BytesIO(src_bytes), 'r') as zin:
        names = zin.namelist()
        new_bytes: Dict[str, bytes] = {}

        for part, segs in translations_by_part.items():
            if part not in names:
                continue
            xml_bytes = zin.read(part)
            parser = lxml_etree.XMLParser(remove_blank_text=False, strip_cdata=False)
            lxml_root = lxml_etree.fromstring(xml_bytes, parser)

            applied = _rebuild.apply_translations_lxml(
                lxml_root, segs,
                metadata.get('choice_paths', {}), metadata.get('fallback_paths', {}),
                metadata.get('fmt_signatures', {}), metadata.get('inline_splits', {}),
                metadata.get('pattern_types', {}),
            )
            logger.info('rebuild %s: applied %d/%d translations', part, applied, len(segs))

            raw = lxml_etree.tostring(lxml_root, xml_declaration=True, encoding='UTF-8', standalone=True)
            raw = raw.replace(
                b"<?xml version='1.0' encoding='UTF-8' standalone='yes'?>",
                b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', 1,
            )
            raw = raw.replace(b'?>\n<', b'?>\r\n<', 1)
            new_bytes[part] = raw

        out_buf = io.BytesIO()
        with zipfile.ZipFile(out_buf, 'w', zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                zout.writestr(name, new_bytes.get(name, zin.read(name)))

    return out_buf.getvalue()


def find_unplaced_translations(rebuilt_bytes: bytes, rows: List[Dict[str, Any]]) -> List[str]:
    """Find segments whose translation is not what the rebuilt document says.

    A translation whose XML path no longer resolves, or whose paragraph has no ``<w:t>``, is skipped silently by the
    rebuild; only re-reading the output reveals it. Whitespace and XML-illegal characters are ignored in the comparison.

    :param rebuilt_bytes: Rebuilt document content.
    :param rows: Segment rows with ``seg_id``, ``translated_text``, ``keep_as_is`` and ``inline_split``.
    :return: Ids of the segments whose translation did not land.
    """
    def norm(text: str) -> str:
        """Normalise text for comparison.

        :param text: Raw text.
        :return: The text without XML-illegal characters and with whitespace collapsed to single spaces.
        """
        return ' '.join(_rebuild._XML_ILLEGAL_RE.sub('', text).split())

    actual = {s['seg_id']: norm(s['text']) for s in extract_docx_segments(rebuilt_bytes)}
    unplaced = []
    for r in rows:
        if r.get('keep_as_is') or not r.get('translated_text') or r['seg_id'] not in actual:
            continue
        expected = norm(r['translated_text'])
        inline = r.get('inline_split')
        span_only = bool(inline and inline.get('span_translated'))
        placed = expected in actual[r['seg_id']] if span_only else expected == actual[r['seg_id']]
        if not placed:
            unplaced.append(r['seg_id'])
    return unplaced


# ---------------------------------------------------------------------------
# validate — 8-check structural integrity suite
# ---------------------------------------------------------------------------

def _check_segment_fidelity_bytes(new_bytes: bytes, reference_segments: List[Dict[str, Any]]) -> List[str]:
    """Compare the segment ids of a rebuilt document with the reference.

    Segments of ``word/comments.xml`` and paragraphs starting with the image-translation marker are ignored: both are
    added deliberately by the rebuild.

    :param new_bytes: Rebuilt document content.
    :param reference_segments: Segments extracted from the source document.
    :return: Error messages; empty when the ids and the count match.
    """
    def _is_excluded(s: Dict[str, Any]) -> bool:
        """Tell whether a segment is excluded from the comparison.

        :param s: Segment dict with ``part`` and ``text``.
        :return: True for comment-part segments and image-translation paragraphs.
        """
        return s['part'] == 'word/comments.xml' or s['text'].lstrip().startswith(_IMAGE_TRANSLATION_MARKER)

    errors = []
    new_segments = [s for s in extract_docx_segments(new_bytes) if not _is_excluded(s)]
    reference_segments = [s for s in reference_segments if not _is_excluded(s)]
    ref_ids = {s['seg_id'] for s in reference_segments}
    new_ids = {s['seg_id'] for s in new_segments}
    missing = ref_ids - new_ids
    extra = new_ids - ref_ids
    if missing:
        errors.append(f'Missing {len(missing)} segments vs reference')
    if extra:
        errors.append(f'Extra {len(extra)} segments vs reference')
    if len(reference_segments) != len(new_segments):
        errors.append(f'Segment count mismatch: ref={len(reference_segments)} new={len(new_segments)}')
    return errors


def _check_byte_identity_bytes(new_bytes: bytes, original_bytes: bytes) -> List[str]:
    """Check that parts the rebuild must not touch are byte-identical to the original.

    :param new_bytes: Rebuilt document content.
    :param original_bytes: Source document content.
    :return: One error per differing part; empty when all match.
    """
    errors = []
    # Footnotes and endnotes are excluded: their paragraphs are translated, so they legitimately change.
    immutable = {
        'word/styles.xml', 'word/settings.xml', 'word/fontTable.xml',
        'word/webSettings.xml', 'word/numbering.xml', 'word/theme/theme1.xml',
    }
    with zipfile.ZipFile(io.BytesIO(new_bytes)) as z1, zipfile.ZipFile(io.BytesIO(original_bytes)) as z2:
        for name in immutable:
            if name not in z1.namelist() or name not in z2.namelist():
                continue
            a, b = z1.read(name), z2.read(name)
            if a != b:
                errors.append(f'{name} differs from original (orig={len(b)} new={len(a)} delta={len(a)-len(b)})')
    return errors


_RESIDUAL_UNIQUE_CHARS = {
    'cs': set('řůěŘŮĚ'),
    'bg': None,  # Cyrillic block check instead
    'de': set('ßÄÖÜäöü'),
    'es': set('ñÑ¿¡'),
    'pt': set('ãõÃÕ'),  # nasal vowels, used by no other language in scope
    'ar': None,  # Arabic block check instead — own script, like bg's Cyrillic
}


def _reads_as_script(text: str, lo: str, hi: str) -> bool:
    """Tell whether text is made of words in a script, not merely containing some of its characters.

    Codes with a look-alike letter ("М8х1,25") and lone proper nouns do not count.

    :param text: Text to test.
    :param lo: First character of the script's code point range.
    :param hi: Last character of the script's code point range.
    :return: True when the text's words are mostly written in the script.
    """
    words = re.findall(r'[^\W\d_]{2,}', text)
    native = [w for w in words if len(w) >= 3 and all(lo <= c <= hi for c in w)]
    if len(words) == 1:
        return len(native) == 1
    return len(native) >= 2 and len(native) * 2 >= len(words)


def _check_residual_source_language_bytes(new_bytes: bytes, source_lang: str) -> List[Dict[str, Any]]:
    """Find the segments of a rebuilt document that still read as the source language.

    This is a quality signal, not a failure.

    :param new_bytes: Rebuilt document content.
    :param source_lang: Source language code.
    :return: Items ``{seg_id, text, detected_lang, confidence}``; ``confidence`` is None when found by script or character.
    """
    segments = extract_docx_segments(new_bytes)
    unique_chars = _RESIDUAL_UNIQUE_CHARS.get(source_lang)
    # Source-exclusive characters miss words whose accents are shared with other languages, so the constrained detector
    # backs them up. source_lang is deliberately not passed to it: its single-word bias toward source_lang would flag
    # ordinary short words kept as-is.
    candidates = {source_lang, 'en', 'fr', 'de', 'es', 'cs', 'bg', 'pt', 'ar'}
    items: List[Dict[str, Any]] = []
    for s in segments:
        text = s['text']
        if not text.strip() or len(text.strip()) < 3:
            continue
        if source_lang == 'bg':
            if _reads_as_script(text, 'Ѐ', 'ӿ'):
                items.append({'seg_id': s['seg_id'], 'text': text,
                              'detected_lang': 'bg', 'confidence': None})
            continue
        if source_lang == 'ar':
            if _reads_as_script(text, '؀', 'ۿ'):
                items.append({'seg_id': s['seg_id'], 'text': text,
                              'detected_lang': 'ar', 'confidence': None})
            continue
        if unique_chars and any(c in unique_chars for c in text):
            items.append({'seg_id': s['seg_id'], 'text': text,
                          'detected_lang': source_lang, 'confidence': None})
            continue
        lang, conf = _langdetect.detect_language_constrained(text, candidates)
        if lang == source_lang and conf >= 0.55:
            items.append({'seg_id': s['seg_id'], 'text': text,
                          'detected_lang': lang, 'confidence': round(conf, 3)})
    return items


def validate_docx(
    new_bytes: bytes,
    original_bytes: bytes | None = None,
    reference_segments: List[Dict[str, Any]] | None = None,
    source_lang: str | None = None,
) -> Dict[str, Any]:
    """Run the validation suite against a rebuilt .docx.

    :param new_bytes: Rebuilt document content.
    :param original_bytes: Source document content; enables the byte-identity check.
    :param reference_segments: Segments of the source document; enables the segment-fidelity check.
    :param source_lang: Source language code; enables the residual-source-language check.
    :return: One ``{'passed', 'errors'}`` entry per check (residual_source_language also has ``items``), plus ``all_passed``.
    """
    checks: List[Tuple[str, List[str]]] = [
        ('zip_integrity', _validate.check_zip_integrity(io.BytesIO(new_bytes))),
        ('xml_validity', _validate.check_xml_validity(io.BytesIO(new_bytes))),
        ('namespace_integrity', _validate.check_namespace_integrity(io.BytesIO(new_bytes))),
        ('auto_prefix', _validate.check_auto_prefixes(io.BytesIO(new_bytes))),
        ('content_types', _validate.check_content_types(io.BytesIO(new_bytes))),
    ]
    if reference_segments is not None:
        checks.append(('segment_fidelity', _check_segment_fidelity_bytes(new_bytes, reference_segments)))
    if original_bytes is not None:
        checks.append(('byte_identity', _check_byte_identity_bytes(new_bytes, original_bytes)))

    results: Dict[str, Any] = {name: {'passed': len(errs) == 0, 'errors': errs} for name, errs in checks}

    # residual_source_language is a quality signal with structured items, kept apart from the string-list structural checks.
    if source_lang:
        items = _check_residual_source_language_bytes(new_bytes, source_lang)
        errs = ([f'{len(items)} segments still contain {source_lang.upper()} text']
                + [f"  {it['seg_id']}: {it['text'][:100]}" for it in items[:10]]
                + ([f'  ... and {len(items) - 10} more'] if len(items) > 10 else []))
        results['residual_source_language'] = {
            'passed': not items, 'errors': errs if items else [], 'items': items,
        }

    results['all_passed'] = all(r['passed'] for r in results.values())
    return results
