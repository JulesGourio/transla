"""
audit.py - Language detection, bilingual pairing, conflict detection,
and question generation.

Input:  reports/<doc>.extract.json
Output: reports/<doc>.audit.json   (one AuditedSegment per input segment)
        reports/<doc>.questions.json (batched questions for the user)

Pairing strategies (applied in priority order, each segment assigned at most
one pair_id):

  1. bilingual_txbx_pair  - within one text box (same txbx_path prefix), two
                            consecutive paragraphs in different languages.
                            This is the dominant pattern in LATECOERE docs.
  2. paired_body          - consecutive body_direct paragraphs in different
                            languages (e.g., "OPERACE 10..." then
                            "OPERATION 10...").
  3. paired_row           - in a table, two adjacent rows where each row is
                            uniformly one language.
  4. bilingual_inline_slash - single segment with "X / Y" pattern, halves
                            in different languages.
  5. bilingual_inline_concat - single segment whose runs split by formatting
                            into two groups detecting different languages.
  6. bilingual_inline_charsplit - single segment concatenating two languages
                            in the SAME run(s), no separator and no formatting
                            break. The boundary is found structurally FIRST
                            (script switch, then sentence boundaries), then
                            each side is language-detected on its own —
                            detect_language on the whole text can only ever
                            return one label.

Segments not paired remain pattern_type = "mono" (or "dnt" / "numeric_only").

Usage:  python audit.py <extract.json>
"""

from __future__ import annotations
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from .langdetect import (SUPPORTED_LANGS, detect_language,
                         detect_language_constrained, detect_span_language,
                         extract_dnt_tokens, is_dnt_candidate, is_numeric_only)
from .glossary_io import DntMatcher, load_dnt_matcher


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-segment classification
# ---------------------------------------------------------------------------

def make_audited(seg: dict, matcher: DntMatcher | None = None,
                 default_lang: str | None = None,
                 candidates: set[str] | None = None,
                 source_lang: str | None = None) -> dict:
    """Classify one extracted segment into an audited-segment dict.

    :param seg: extracted segment dict (text, seg_id, location and run fields).
    :param matcher: reviewer-curated DNT rules applied on top of the built-in DNT regex; None means built-in rules only.
    :param default_lang: the document's dominant language, used as tie-break when the text carries no language signal; None means no bias.
    :param candidates: language codes the detector may return (the job's declared pair plus languages found in the document); None means the free detector over all supported languages.
    :param source_lang: the job's source language, used by the constrained detector to resolve ties; None means no preference.
    :return: audited segment dict, including private underscore fields consumed by the pairing passes.
    """
    text = seg["text"]
    if is_dnt_candidate(text, matcher):
        lang, conf = "??", 0.0
        pattern = "dnt"
    elif is_numeric_only(text):
        lang, conf = "??", 0.0
        pattern = "numeric_only"
    elif candidates:
        lang, conf = detect_language_constrained(text, candidates, default_lang=default_lang, source_lang=source_lang)
        pattern = "mono"
    else:
        lang, conf = detect_language(text, default_lang=default_lang)
        pattern = "mono"
    return {
        "seg_id": seg["seg_id"],
        "text": text,
        "detected_lang": lang,
        "lang_confidence": round(conf, 3),
        "pattern_type": pattern,
        "pair_id": None,
        "pair_role": None,
        "richer_side": None,
        "conflict_flag": False,
        "conflict_detail": None,
        "dnt_tokens": extract_dnt_tokens(text, matcher),
        "notes": "",
        "inline_split": None,
        # Private fields feed the pairing logic and are stripped from the audit output.
        "_part": seg["part"],
        "_location": seg["location_type"],
        "_body_p_idx": seg["body_p_idx"],
        "_table_coords": seg.get("table_coords"),
        "_txbx_path": seg.get("txbx_path"),
        "_para_idx": seg["para_idx_in_container"],
        "_fmt_signature": seg["fmt_signature"],
        "_runs": seg["runs"],
    }


# Minimum confidence for a detection to decide translate-or-keep. Constrained
# detection reaches it only when both the heuristic and fastText agree.
CONFIDENT = 0.85


def _significant_languages(audited: list[dict]) -> set[str]:
    """Languages the document actually contains, by confident mono-segment count.

    The kept language is often a third language (English labels in a BG->FR job), so it must be a detection candidate to be recognised and kept.

    :param audited: audited segments from a first detection pass.
    :return: language codes with at least 3 confident segments and at least 5% of all confident segments; empty when there are none.
    """
    counts = Counter(
        a["detected_lang"] for a in audited
        if a["pattern_type"] == "mono" and a["detected_lang"] != "??"
        and a["lang_confidence"] >= CONFIDENT
    )
    total = sum(counts.values())
    if total == 0:
        return set()
    return {lang for lang, c in counts.items() if c >= 3 and c >= 0.05 * total}


def audit_all_segments(segments: list[dict], matcher: DntMatcher | None = None,
                       source_lang: str | None = None,
                       target_lang: str | None = None) -> list[dict]:
    """Detect each mono segment's language and bias zero-signal segments to the document's dominant language.

    With source and target given, a first pass over every supported language discovers the languages the document holds, then detection is constrained to the pair plus those languages. Without a pair, the free heuristic detector is used.

    :param segments: extracted segment dicts.
    :param matcher: reviewer-curated DNT rules; None means built-in rules only.
    :param source_lang: declared source language code; None selects the free detector.
    :param target_lang: declared target language code; None selects the free detector.
    :return: one audited segment dict per input segment, in the same order.
    """
    if source_lang and target_lang:
        # Pass A: full-set detection to learn the document's real languages.
        audited = [make_audited(s, matcher, candidates=set(SUPPORTED_LANGS)) for s in segments]
        candidates = {source_lang, target_lang} | _significant_languages(audited)
        # Pass B: re-detect constrained to the pair plus the document's languages.
        audited = [make_audited(s, matcher, candidates=candidates, source_lang=source_lang) for s in segments]
    else:
        candidates = None
        audited = [make_audited(s, matcher) for s in segments]

    lang_counts = Counter(
        a["detected_lang"] for a in audited
        if a["pattern_type"] == "mono" and a["detected_lang"] != "??"
    )
    if not lang_counts:
        return audited
    dominant = lang_counts.most_common(1)[0][0]
    for i, (s, a) in enumerate(zip(segments, audited)):
        if a["pattern_type"] == "mono" and a["detected_lang"] == "??":
            audited[i] = make_audited(s, matcher, default_lang=dominant, candidates=candidates)
    return audited


def determine_mode(audited: list[dict], source_lang: str, target_lang: str) -> str:
    """Decide whether a document is monolingual or bilingual relative to the source language.

    Compares source-language content against all confident non-source content, not against the target, because the kept language is often a third one. Only confident detections count.

    :param audited: audited segments.
    :param source_lang: source language code.
    :param target_lang: target language code.
    :return: "bilingual" when meaningful content in another language must be kept, otherwise "monolingual".
    """
    src = sum(1 for a in audited if a["pattern_type"] == "mono"
              and a["detected_lang"] == source_lang and a["lang_confidence"] >= CONFIDENT)
    other = sum(1 for a in audited if a["pattern_type"] == "mono"
                and a["detected_lang"] not in ("??", source_lang)
                and a["lang_confidence"] >= CONFIDENT)
    total = src + other
    if total == 0:
        return "monolingual"
    if other >= 5 and other >= 0.15 * total:
        return "bilingual"
    return "monolingual"


# ---------------------------------------------------------------------------
# Pairing strategies
# ---------------------------------------------------------------------------

class Pairer:
    def __init__(self) -> None:
        """Start the pair id counter at 1."""
        self.next_id = 1

    def assign(self, primary: dict, secondary: dict, pattern_type: str) -> None:
        """Link two segments as one bilingual pair, unless either is already paired.

        :param primary: segment taking the "primary" role; mutated in place.
        :param secondary: segment taking the "secondary" role; mutated in place.
        :param pattern_type: pattern label written on both segments.
        """
        if primary["pair_id"] or secondary["pair_id"]:
            return
        pair_id = f"pair_{self.next_id:04d}"
        self.next_id += 1
        for seg, role in ((primary, "primary"), (secondary, "secondary")):
            seg["pair_id"] = pair_id
            seg["pair_role"] = role
            seg["pattern_type"] = pattern_type


def _format_groups(runs: list[dict]) -> list[tuple[str, str]]:
    """Group consecutive runs sharing a format hash.

    :param runs: run dicts with "fmt_hash" and "text" keys.
    :return: (fmt_hash, joined_text) pairs for groups whose text is non-blank, in run order.
    """
    if not runs:
        return []
    groups = []
    cur_fmt = runs[0]["fmt_hash"]
    cur_text = ""
    for r in runs:
        if r["fmt_hash"] == cur_fmt:
            cur_text += r["text"]
        else:
            if cur_text.strip():
                groups.append((cur_fmt, cur_text))
            cur_fmt = r["fmt_hash"]
            cur_text = r["text"]
    if cur_text.strip():
        groups.append((cur_fmt, cur_text))
    return groups


def pair_txbx_siblings(audited: list[dict], pairer: Pairer) -> None:
    """Pair consecutive paragraphs of one text box that are in different languages.

    :param audited: audited segments; paired members are mutated in place.
    :param pairer: pair id allocator.
    """
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for seg in audited:
        if seg["_location"] != "txbx" or seg["pair_id"]:
            continue
        if seg["pattern_type"] in ("dnt", "numeric_only"):
            continue
        tx = seg["_txbx_path"]
        key = (seg["_part"], tx.get("container"), tx["body_p_idx"], tx["ac_idx"], tx["txbx_idx"])
        groups[key].append(seg)
    for key, segs in groups.items():
        segs.sort(key=lambda s: s["_para_idx"])
        i = 0
        while i < len(segs) - 1:
            a, b = segs[i], segs[i + 1]
            if (a["detected_lang"] != b["detected_lang"]
                    and a["detected_lang"] != "??"
                    and b["detected_lang"] != "??"
                    and not a["pair_id"] and not b["pair_id"]):
                pairer.assign(a, b, "bilingual_txbx_pair")
                i += 2
            else:
                i += 1


def pair_adjacent_body(audited: list[dict], pairer: Pairer) -> None:
    """Pair body paragraphs in different languages that are adjacent (up to two empty paragraphs between) in the same XML part.

    :param audited: audited segments; paired members are mutated in place.
    :param pairer: pair id allocator.
    """
    by_part: dict[str, list[dict]] = defaultdict(list)
    for seg in audited:
        if seg["_location"] != "body_direct" or seg["pair_id"]:
            continue
        if seg["pattern_type"] in ("dnt", "numeric_only"):
            continue
        by_part[seg["_part"]].append(seg)
    for part, segs in by_part.items():
        segs.sort(key=lambda s: s["_body_p_idx"])
        i = 0
        while i < len(segs) - 1:
            a, b = segs[i], segs[i + 1]
            gap = b["_body_p_idx"] - a["_body_p_idx"]
            if (1 <= gap <= 3
                    and a["detected_lang"] != b["detected_lang"]
                    and a["detected_lang"] != "??"
                    and b["detected_lang"] != "??"
                    and not a["pair_id"] and not b["pair_id"]):
                pairer.assign(a, b, "paired_body")
                i += 2
            else:
                i += 1


def pair_table_rows(audited: list[dict], pairer: Pairer) -> None:
    """Pair adjacent table rows that are each uniformly one language, cell by cell.

    :param audited: audited segments; paired members are mutated in place.
    :param pairer: pair id allocator.
    """
    by_table: dict[tuple, dict[int, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for seg in audited:
        if seg["_location"] != "table" or seg["pair_id"]:
            continue
        if seg["pattern_type"] in ("dnt", "numeric_only"):
            continue
        tc = seg["_table_coords"]
        by_table[(seg["_part"], tc["table_body_idx"])][tc["row_idx"]].append(seg)

    for (part, tbl_idx), rows in by_table.items():
        # Determine each row's dominant language
        row_langs: dict[int, str] = {}
        for row_idx, segs in rows.items():
            langs = Counter(s["detected_lang"] for s in segs
                            if s["detected_lang"] != "??")
            if langs:
                row_langs[row_idx] = langs.most_common(1)[0][0]
        sorted_row_idxs = sorted(row_langs.keys())
        used: set = set()
        for i in range(len(sorted_row_idxs) - 1):
            r1 = sorted_row_idxs[i]
            r2 = sorted_row_idxs[i + 1]
            if r1 in used or r2 in used:
                continue
            if r2 - r1 != 1:
                continue
            if row_langs[r1] != row_langs[r2]:
                # Pair cells column-by-column
                for s1 in rows[r1]:
                    for s2 in rows[r2]:
                        if (s1["_table_coords"]["cell_idx"]
                                == s2["_table_coords"]["cell_idx"]):
                            if not s1["pair_id"] and not s2["pair_id"]:
                                pairer.assign(s1, s2, "paired_row")
                used.add(r1)
                used.add(r2)


SLASH_SPLIT_RE = re.compile(r"^(.+?)\s*/\s*(.+)$")


def pair_inline_slash(audited: list[dict]) -> None:
    """Mark mono segments shaped "X / Y" whose halves are in different languages.

    :param audited: audited segments; matches are mutated in place (pattern_type and inline_split).
    """
    for seg in audited:
        if seg["pair_id"] or seg["pattern_type"] != "mono":
            continue
        m = SLASH_SPLIT_RE.match(seg["text"])
        if not m:
            continue
        left, right = m.group(1).strip(), m.group(2).strip()
        if len(left) < 3 or len(right) < 3:
            continue
        lang_l, conf_l = detect_span_language(left)
        lang_r, conf_r = detect_span_language(right)
        if (lang_l != lang_r and lang_l != "??" and lang_r != "??"
                and min(conf_l, conf_r) > 0.5):
            seg["pattern_type"] = "bilingual_inline_slash"
            # Compute the offset of the "/" in the original text
            slash_idx = seg["text"].index("/")
            seg["inline_split"] = {
                "kind": "slash",
                "offset": slash_idx,
                "left_lang": lang_l,
                "right_lang": lang_r,
            }


def pair_inline_format_concat(audited: list[dict]) -> None:
    """Mark mono segments whose two largest format groups are in different languages.

    :param audited: audited segments; matches are mutated in place (pattern_type and inline_split).
    """
    for seg in audited:
        if seg["pair_id"] or seg["pattern_type"] != "mono":
            continue
        groups = _format_groups(seg["_runs"])
        if len(groups) < 2:
            continue
        # Take the two largest groups
        groups_sorted = sorted(groups, key=lambda g: -len(g[1]))[:2]
        (fmt_a, txt_a), (fmt_b, txt_b) = groups_sorted
        lang_a, conf_a = detect_span_language(txt_a)
        lang_b, conf_b = detect_span_language(txt_b)
        if (lang_a != lang_b and lang_a != "??" and lang_b != "??"
                and min(conf_a, conf_b) > 0.6):
            seg["pattern_type"] = "bilingual_inline_concat"
            # Find the run index where format changes from group A to B
            split_idx = None
            for i, r in enumerate(seg["_runs"]):
                if r["fmt_hash"] == fmt_b:
                    split_idx = i
                    break
            seg["inline_split"] = {
                "kind": "format",
                "run_split_idx": split_idx,
                "fmt_a": fmt_a,
                "fmt_b": fmt_b,
                "lang_a": lang_a,
                "lang_b": lang_b,
                # Side texts let the translation stage send only the source-language side to the LLM.
                "text_a": txt_a,
                "text_b": txt_b,
            }


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?;:…])\s+|\n+")


_UNIT_SUFFIX_RE = re.compile(r"\d[\d.,]*\s?([A-Za-z°µ]{1,3})(?![A-Za-z])")


def _script_block_boundary(text: str) -> int | None:
    """Find where a Cyrillic block meets a Latin block (or vice versa) in a text.

    Short Latin unit suffixes glued to a digit ("35 Nm", "3rd") carry no language signal and are ignored.

    :param text: segment text.
    :return: offset where the second script block starts, or None when scripts interleave or a side has fewer than two letters.
    """
    unit_idx: set[int] = set()
    for m in _UNIT_SUFFIX_RE.finditer(text):
        unit_idx.update(range(m.start(1), m.end(1)))

    cyr_pos = []
    lat_pos = []
    for i, c in enumerate(text):
        if not c.isalpha() or i in unit_idx:
            continue
        if "Ѐ" <= c <= "ӿ":
            cyr_pos.append(i)
        else:
            lat_pos.append(i)
    if len(cyr_pos) < 2 or len(lat_pos) < 2:
        return None
    if cyr_pos[-1] < lat_pos[0]:
        return lat_pos[0]
    if lat_pos[-1] < cyr_pos[0]:
        return cyr_pos[0]
    return None


def _find_char_boundary(text: str) -> tuple[int, str, str, float] | None:
    """Find the language boundary of a two-language concatenation.

    Uses structural signals only: a script switch first, sentence boundaries otherwise.

    :param text: segment text.
    :return: (offset of the right-hand span, left language, right language, minimum confidence), or None for single-language or interleaved text.
    """
    p = _script_block_boundary(text)
    if p is not None:
        lang_l, conf_l = detect_span_language(text[:p])
        lang_r, conf_r = detect_span_language(text[p:])
        if (lang_l != lang_r and lang_l != "??" and lang_r != "??"
                and min(conf_l, conf_r) > 0.5):
            return p, lang_l, lang_r, min(conf_l, conf_r)
        return None

    best: tuple[int, str, str, float] | None = None
    for m in _SENTENCE_BOUNDARY_RE.finditer(text):
        p = m.end()
        if not (0 < p < len(text)):
            continue
        left, right = text[:p], text[p:]
        if (sum(1 for c in left if c.isalpha()) < 4
                or sum(1 for c in right if c.isalpha()) < 4):
            continue
        lang_l, conf_l = detect_span_language(left)
        lang_r, conf_r = detect_span_language(right)
        if lang_l == lang_r or lang_l == "??" or lang_r == "??":
            continue
        score = min(conf_l, conf_r)
        if score >= 0.55 and (best is None or score > best[3]):
            best = (p, lang_l, lang_r, score)
    return best


def pair_inline_charspan(audited: list[dict]) -> None:
    """Mark mono segments concatenating two languages in the same runs, with no separator and no formatting break.

    :param audited: audited segments; matches are mutated in place (pattern_type and inline_split).
    """
    for seg in audited:
        if seg["pair_id"] or seg["pattern_type"] != "mono":
            continue
        found = _find_char_boundary(seg["text"])
        if not found:
            continue
        offset, lang_l, lang_r, conf = found
        seg["pattern_type"] = "bilingual_inline_charsplit"
        seg["inline_split"] = {
            "kind": "char",
            "offset": offset,
            "left_lang": lang_l,
            "right_lang": lang_r,
            "confidence": round(conf, 3),
        }


# ---------------------------------------------------------------------------
# Richness + conflict detection
# ---------------------------------------------------------------------------

NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def normalize_numbers(text: str) -> set[str]:
    """Extract the numbers of a text with "." as decimal separator.

    :param text: text to scan.
    :return: set of number strings.
    """
    out = set()
    for n in NUMBER_RE.findall(text):
        out.add(n.replace(",", "."))
    return out


def assess_pair(primary: dict, secondary: dict) -> None:
    """Set richer_side, conflict_flag and conflict_detail on both members of a pair.

    Conflict reasons are worded for end users, not for engineers.

    :param primary: primary pair member; mutated in place.
    :param secondary: secondary pair member; mutated in place.
    """
    # Richer side: more words wins (ties = "equal")
    wp = len(primary["text"].split())
    ws = len(secondary["text"].split())
    if wp > ws * 1.15:
        richer = "primary"
    elif ws > wp * 1.15:
        richer = "secondary"
    else:
        richer = "equal"

    # Reasons are shown to end users, so they avoid technical terms.
    reasons = []
    dnt_p, dnt_s = set(primary["dnt_tokens"]), set(secondary["dnt_tokens"])
    if dnt_p - dnt_s or dnt_s - dnt_p:
        reasons.append("a reference code or part number appears on one side but not the other")
    nums_p = normalize_numbers(primary["text"])
    nums_s = normalize_numbers(secondary["text"])
    if nums_p - nums_s or nums_s - nums_p:
        reasons.append("the numbers mentioned don't match on both sides")
    if max(wp, ws) > 5 and (wp / max(ws, 1) > 2.5 or ws / max(wp, 1) > 2.5):
        reasons.append("one side is much shorter than the other, so it may be missing information")

    detail = (
        "These two texts were matched together, but " + "; and ".join(reasons) + "."
        if reasons else None
    )
    flag = bool(reasons)
    for seg in (primary, secondary):
        seg["richer_side"] = richer
        seg["conflict_flag"] = flag
        seg["conflict_detail"] = detail


# ---------------------------------------------------------------------------
# Determine primary/secondary languages doc-wide
# ---------------------------------------------------------------------------

def determine_languages(audited: list[dict]) -> tuple[str, str, list[str]]:
    """Rank the languages detected across the document.

    :param audited: audited segments.
    :return: (primary language, secondary language, all detected languages by frequency); "??" stands for a missing language.
    """
    langs = Counter(s["detected_lang"] for s in audited
                    if s["detected_lang"] != "??"
                    and s["pattern_type"] not in ("dnt", "numeric_only"))
    most = langs.most_common()
    if len(most) == 0:
        return "??", "??", []
    if len(most) == 1:
        return most[0][0], "??", [most[0][0]]
    return most[0][0], most[1][0], [l for l, _ in most]


# ---------------------------------------------------------------------------
# Question generation
# ---------------------------------------------------------------------------

def generate_questions(audited: list[dict], primary_lang: str,
                       secondary_lang: str, source_file: str,
                       mode: str = "bilingual") -> list[dict]:
    """Generate clarifying questions with rules (no LLM).

    Only bilingual documents get questions, about low-confidence language detections. Pair conflicts are not questions; they stay visible as flags.

    :param audited: audited segments.
    :param primary_lang: primary language code, used in the question text.
    :param secondary_lang: secondary language code, used in the question text.
    :param source_file: source file name.
    :param mode: "bilingual" or "monolingual"; monolingual yields no questions.
    :return: question dicts, at most five.
    """
    questions = []
    qid = 1

    def add(category: str, question_text: str, seg_ids: list[str] | None = None,
            context: str = "", suggested: str | None = None) -> None:
        """Append one question to the enclosing list.

        :param category: question category.
        :param question_text: text shown to the user.
        :param seg_ids: segment ids the question concerns; None means none.
        :param context: text excerpt shown with the question.
        :param suggested: suggested answer; None means no suggestion.
        """
        nonlocal qid
        questions.append({
            "q_id": f"q_{qid:03d}",
            "seg_ids": seg_ids or [],
            "category": category,
            "question_text": question_text,
            "context": context,
            "suggested_answer": suggested,
        })
        qid += 1

    # Monolingual documents translate every segment, so there is nothing to clarify.
    if mode != "bilingual":
        return questions

    # Ambiguous detections decide translate-vs-keep in bilingual mode.
    low_conf = [s for s in audited
                if s["pattern_type"] == "mono"
                and s["lang_confidence"] < 0.55
                and len(s["text"].split()) > 1]
    for s in low_conf[:5]:
        add("unknown_lang",
            f"We're not sure what language this text is written in — "
            f"is it {primary_lang.upper()}, {secondary_lang.upper()}, or something else?",
            seg_ids=[s["seg_id"]],
            context=s["text"])

    return questions


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def strip_internal(seg: dict) -> dict:
    """Remove the private underscore-prefixed fields of an audited segment.

    :param seg: audited segment dict.
    :return: a copy without underscore-prefixed keys.
    """
    return {k: v for k, v in seg.items() if not k.startswith("_")}


def main(extract_path: str, _job_dir: Path | None = None) -> None:
    """Audit an extract file and write the audit and questions JSON next to it.

    :param extract_path: path of the extract JSON.
    :param _job_dir: job directory whose manifest status is set to "audited"; None skips the manifest update.
    """
    src = Path(extract_path)
    extract = json.loads(src.read_text(encoding="utf-8"))

    matcher = load_dnt_matcher()
    audited = audit_all_segments(extract["segments"], matcher)

    # Run pairing strategies in order
    pairer = Pairer()
    pair_txbx_siblings(audited, pairer)
    pair_adjacent_body(audited, pairer)
    pair_table_rows(audited, pairer)
    pair_inline_slash(audited)
    pair_inline_format_concat(audited)
    pair_inline_charspan(audited)

    # Compute per-pair richness + conflicts
    pairs_by_id: dict[str, list[dict]] = defaultdict(list)
    for s in audited:
        if s["pair_id"]:
            pairs_by_id[s["pair_id"]].append(s)
    for pid, segs in pairs_by_id.items():
        if len(segs) != 2:
            continue
        primary = next((s for s in segs if s["pair_role"] == "primary"), None)
        secondary = next((s for s in segs if s["pair_role"] == "secondary"), None)
        if primary and secondary:
            assess_pair(primary, secondary)

    primary_lang, secondary_lang, all_langs = determine_languages(audited)

    # Build outputs
    audit_report = {
        "source_file": extract["source_file"],
        "detected_languages": all_langs,
        "primary_language": primary_lang,
        "secondary_language": secondary_lang,
        "segment_count": len(audited),
        "pair_count": len([1 for s in audited if s["pair_role"] == "primary"]),
        "conflict_count": len([1 for s in audited
                               if s["pair_role"] == "primary" and s["conflict_flag"]]),
        "dnt_token_count": sum(len(s["dnt_tokens"]) for s in audited),
        "segments": [strip_internal(s) for s in audited],
    }

    questions = generate_questions(audited, primary_lang, secondary_lang,
                                   extract["source_file"])
    questions_file = {
        "source_file": extract["source_file"],
        "question_count": len(questions),
        "questions": questions,
    }

    # Write outputs alongside the input
    base = src.stem.replace(".extract", "")
    out_dir = src.parent
    audit_out = out_dir / f"{base}.audit.json"
    questions_out = out_dir / f"{base}.questions.json"
    audit_out.write_text(json.dumps(audit_report, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    questions_out.write_text(json.dumps(questions_file, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # Summary
    pattern_counts = Counter(s["pattern_type"] for s in audit_report["segments"])
    logger.info("Audited %d segments -> %s", len(audited), audit_out.name)
    logger.info("Languages detected: %s", all_langs)
    logger.info("Primary=%s Secondary=%s", primary_lang.upper(), secondary_lang.upper())
    logger.info("Pair count: %s", audit_report["pair_count"])
    logger.info("Conflicts: %s", audit_report["conflict_count"])
    logger.info("Patterns: %s", dict(pattern_counts))
    logger.info("DNT tokens (occurrences): %s", audit_report["dnt_token_count"])
    logger.info("Generated %d questions -> %s", len(questions), questions_out.name)

    if _job_dir:
        from job_utils import update_manifest
        update_manifest(_job_dir, status="audited")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    from job_utils import parse_job_flag, JobPaths
    job_dir, remaining = parse_job_flag(sys.argv[1:])
    if job_dir:
        jp = JobPaths(job_dir)
        main(str(jp.extract_json), _job_dir=job_dir)
    else:
        if len(sys.argv) != 2:
            logger.error("Usage: python audit.py <extract.json>")
            sys.exit(1)
        main(sys.argv[1])
