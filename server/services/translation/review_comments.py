"""Generate Word review-comment specs from a job's already-computed data.

Every category reuses a signal the pipeline already computes: conflict_detail
(audit.py's assess_pair), the segment category buckets shown in the Segments
panel, the bilingual-inline pattern_type, and the fit check's flagged list.

comments.py anchors these specs into the .docx as real Word comments.
"""
from __future__ import annotations

from typing import Any

_LOCATION_LABELS = {
    'header': 'Header/footer', 'footer': 'Header/footer', 'txbx': 'Text box',
    'table': 'Table cell', 'body_direct': 'Body text', 'sdt': 'Content control',
}

# Lowest value wins: one comment per segment keeps the Review pane readable.
_TYPE_PRIORITY = {'header': 0, 'untranslated': 1, 'conflict': 2, 'overflow': 3, 'layout': 4}

_INLINE_PATTERNS = ('bilingual_inline_slash', 'bilingual_inline_concat', 'bilingual_inline_charsplit')

_AUTHOR = 'Translation review'


def generate_review_comments(
    segments: list[dict[str, Any]], flagged_fit: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Generate Word review-comment specs from a job's already-computed data.

    At most one comment per segment, the highest-priority type winning.

    :param segments: one dict per segment with at least seg_id, pattern_type, conflict_flag, conflict_detail and category (translated, kept_dnt, kept_numeric, kept_other_language or needs_review).
    :param flagged_fit: flagged overflow entries as returned by the fit check.
    :return: [{seg_id, author, type, comment}, ...] ready for comments.inject_comments.
    """
    comments: list[dict[str, str]] = []

    for s in segments:
        if s.get('conflict_flag') and s.get('conflict_detail'):
            comments.append({
                'seg_id': s['seg_id'], 'author': _AUTHOR, 'type': 'conflict',
                'comment': f"{s['conflict_detail']} Please verify against the original.",
            })

    for s in segments:
        if s.get('category') == 'needs_review':
            comments.append({
                'seg_id': s['seg_id'], 'author': _AUTHOR, 'type': 'untranslated',
                'comment': ('This segment could not be translated automatically after retries. '
                            'Source text was kept as-is — please provide the translation or '
                            'confirm it should stay untranslated.'),
            })

    for s in segments:
        if s.get('pattern_type') in _INLINE_PATTERNS:
            comments.append({
                'seg_id': s['seg_id'], 'author': _AUTHOR, 'type': 'layout',
                'comment': ('This paragraph mixed both languages together in the source. '
                            'Please verify both the kept and translated portions read correctly.'),
            })

    for f in flagged_fit:
        label = _LOCATION_LABELS.get(f['location'], f['location'].replace('_', ' '))
        ctype = 'header' if f['location'] in ('header', 'footer') else 'overflow'
        comments.append({
            'seg_id': f['seg_id'], 'author': _AUTHOR, 'type': ctype,
            'comment': (f"{label}: translated text is {f['ratio']}% of the original length "
                        f"({f['new_len']} vs {f['orig_len']} chars, threshold {f['threshold']}%). "
                        f"May overflow its container — consider shortening or check the layout."),
        })

    seen: set = set()
    ordered: list[dict[str, str]] = []
    for c in sorted(comments, key=lambda c: _TYPE_PRIORITY.get(c['type'], 9)):
        if c['seg_id'] in seen:
            continue
        seen.add(c['seg_id'])
        ordered.append(c)
    return ordered
