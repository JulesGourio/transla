"""Per-segment translation decisions: what is sent to the LLM, what is kept, and the reviewer-facing category."""

import json
import re
from typing import Any, Dict, Optional


LANG_ALIASES = {
    'en': ('en', 'english', 'anglais'),
    'fr': ('fr', 'french', 'français', 'francais'),
    'es': ('es', 'spanish', 'espagnol'),
    'de': ('de', 'german', 'allemand'),
    'cs': ('cs', 'czech', 'tchèque', 'tcheque'),
    'bg': ('bg', 'bulgarian', 'bulgare'),
    'pt': ('pt', 'portuguese', 'portugais'),
    'ar': ('ar', 'arabic', 'arabe'),
}

def parse_lang_answer(answer: str) -> Optional[str]:
    """Extract exactly one supported language from a free-text answer such as 'czech' or 'CS'.

    :param answer: reviewer's answer; may be empty or None.
    :return: language code, or None when the answer names no language or several.
    """
    words = set(re.findall(r'[a-zà-ÿ]+', (answer or '').lower()))
    matches = {code for code, names in LANG_ALIASES.items() if words & set(names)}
    return matches.pop() if len(matches) == 1 else None


INLINE_PATTERNS = ('bilingual_inline_slash', 'bilingual_inline_concat',
                    'bilingual_inline_charsplit')


def split_ws(text: str) -> tuple[str, str, str]:
    """Split a text into its surrounding whitespace and core.

    :param text: text to split.
    :return: (leading_whitespace, core, trailing_whitespace); their concatenation equals text.
    """
    lead = len(text) - len(text.lstrip())
    trail = len(text) - len(text.rstrip())
    return text[:lead], text[lead:len(text) - trail], text[len(text) - trail:] if trail else ''


# Keep a whole mono segment untranslated (in monolingual mode) only when the
# constrained detector is CONFIDENT (agreement-backed, see audit.CONFIDENT) it
# is a language OTHER than the source AND the segment is long enough that the
# label is trustworthy. The kept language is whatever is NOT the source —
# usually a third language (English part-numbers/labels), not the declared
# target. Short confident-but-maybe-wrong labels (a cognate title) fall below
# this and get translated instead of silently kept.
KEEP_OTHER_CONF = 0.85
KEEP_OTHER_MIN_WORDS = 5


def plan_segment_translation(row: Dict[str, Any], source_lang: str,
                             target_lang: str, mode: str) -> Optional[Dict[str, Any]]:
    """Decide what one segment sends to the LLM, or that it is kept as is.

    :param row: segment fields: pattern_type, source_text, inline_split (dict or None), detected_lang, lang_confidence, out_of_page_range.
    :param source_lang: declared source language code.
    :param target_lang: declared target language code.
    :param mode: document mode, 'monolingual' or 'bilingual'.
    :return: None to keep the segment untouched, else a dict with 'query' (string to translate), 'compose' (maps the translation to the stored translated_text), 'kept_text' (verbatim text absent from the stored value, '' unless a format split) and 'inline_json' (updated inline_split JSON to persist, None when unchanged).
    """
    pattern = row['pattern_type']
    if pattern in ('dnt', 'numeric_only') or row.get('out_of_page_range'):
        return None
    text = row['source_text']
    inline = row.get('inline_split')
    if pattern in INLINE_PATTERNS and isinstance(inline, dict):
        kind = inline.get('kind')
        if kind in ('char', 'slash'):
            off = inline.get('offset')
            sep_len = 1 if kind == 'slash' else 0
            if (isinstance(off, int) and 0 < off < len(text)
                    and (kind != 'slash' or text[off] == '/')):
                left_raw, sep, right_raw = text[:off], text[off:off + sep_len], text[off + sep_len:]
                for side_raw, lang, is_left in ((left_raw, inline.get('left_lang'), True),
                                                (right_raw, inline.get('right_lang'), False)):
                    if lang != source_lang:
                        continue
                    lead, core, trail = split_ws(side_raw)
                    if not core:
                        break
                    compose = (
                        (lambda tr, _l=lead, _t=trail, _s=sep, _o=right_raw: f'{_l}{tr}{_t}{_s}{_o}')
                        if is_left else
                        (lambda tr, _l=lead, _t=trail, _s=sep, _o=left_raw: f'{_o}{_s}{_l}{tr}{_t}')
                    )
                    return {'query': core, 'compose': compose, 'kept_text': '', 'inline_json': None}
                return None  # neither side is the source language: keep whole
            # Malformed/drifted split metadata: whole-text fallback below.
        elif kind == 'format':
            text_a, text_b = inline.get('text_a'), inline.get('text_b')
            if isinstance(text_a, str) and isinstance(text_b, str):
                for span, lang, fmt, kept in ((text_a, inline.get('lang_a'), inline.get('fmt_a'), text_b),
                                              (text_b, inline.get('lang_b'), inline.get('fmt_b'), text_a)):
                    if lang != source_lang:
                        continue
                    core = span.strip()
                    if not core:
                        break
                    updated = dict(inline)
                    updated.update({'span_translated': True,
                                    'source_fmt': fmt or '',
                                    'source_span_len': len(span)})
                    return {'query': core, 'compose': (lambda tr: tr),
                            'kept_text': kept, 'inline_json': json.dumps(updated)}
                return None
            # Rows audited before side texts were recorded: fallback below.
    if mode == 'monolingual':
        # Translate everything except a confidently-detected OTHER-language
        # passage (any language that isn't the source — usually English, not
        # the declared target). Undetermined / low-confidence / source-language
        # all get translated — no silent keep on a guessed label.
        conf = row.get('lang_confidence') or 0.0
        dl = row['detected_lang']
        if (dl not in (source_lang, '??', None) and conf >= KEEP_OTHER_CONF
                and len(text.split()) >= KEEP_OTHER_MIN_WORDS):
            return None
        return {'query': text, 'compose': (lambda tr: tr), 'kept_text': '', 'inline_json': None}
    # bilingual: source-language side only
    if row['detected_lang'] == source_lang:
        return {'query': text, 'compose': (lambda tr: tr), 'kept_text': '', 'inline_json': None}
    return None


def length_anomaly(source: str, translated: str) -> Optional[str]:
    """Detect a translation that is implausibly short or long compared with its source.

    :param source: source text sent to the LLM.
    :param translated: translation received.
    :return: a reviewer-facing message, or None when the lengths are plausible.
    """
    n_src, n_tr = len(source.strip()), len(translated.strip())
    if n_src >= 60 and n_tr < 0.3 * n_src:
        return 'Translation looks incomplete (much shorter than the source)'
    if n_src >= 40 and n_tr > 3 * n_src:
        return 'Translation looks too long (text may have been added)'
    return None


def segment_category(row: Dict[str, Any]) -> str:
    """Reviewer-facing outcome bucket of one segment.

    :param row: segment row with location_type, out_of_page_range, pattern_type, translated_text, keep_as_is and conflict_flag.
    :return: one of 'image_translation', 'kept_page_filtered', 'kept_dnt', 'kept_numeric', 'translated', 'needs_review', 'kept_other_language' or 'pending'.
    """
    if row.get('location_type') == 'docx_image_ocr':
        return 'image_translation'
    if row.get('out_of_page_range'):
        return 'kept_page_filtered'
    if row['pattern_type'] == 'dnt':
        return 'kept_dnt'
    if row['pattern_type'] == 'numeric_only':
        return 'kept_numeric'
    if row['translated_text'] is not None:
        return 'translated'
    if row['keep_as_is']:
        # A failed translation is flagged (conflict_flag) when kept; an
        # intentional keep is not.
        return 'needs_review' if row.get('conflict_flag') else 'kept_other_language'
    return 'pending'
