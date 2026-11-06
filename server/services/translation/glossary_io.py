"""
glossary_io.py - Unified multi-language glossary and pattern-based DNT list.

Glossary: glossary/terms.csv — one row per concept, one column per language.
    term_id, en, fr, cs, bg, de, es, domain, notes, added_date

DNT: glossary/dnt.csv — pattern-based do-not-translate rules.
    pattern, type, match_mode, notes
    match_mode: exact | prefix | glob | regex
"""

from __future__ import annotations
import csv
import fnmatch
import logging
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

GLOSSARY_DIR = Path(__file__).resolve().parent.parent / "glossary"
TERMS_FILE = GLOSSARY_DIR / "terms.csv"
DNT_FILE = GLOSSARY_DIR / "dnt.csv"

LANGUAGES = ["en", "fr", "cs", "bg", "de", "es"]
TERMS_FIELDS = ["term_id"] + LANGUAGES + ["domain", "notes", "added_date"]
DNT_FIELDS = ["pattern", "type", "match_mode", "notes"]


# ---------------------------------------------------------------------------
# Unified glossary
# ---------------------------------------------------------------------------

def load_glossary() -> list[dict]:
    """Load all glossary terms from the CSV file.

    :return: one dict per row; empty when the file does not exist.
    """
    if not TERMS_FILE.exists():
        return []
    rows = []
    with TERMS_FILE.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def save_glossary(rows: list[dict]) -> None:
    """Overwrite the glossary CSV with the given rows, sorted by term_id.

    :param rows: row dicts; keys outside the glossary columns are dropped.
    """
    GLOSSARY_DIR.mkdir(parents=True, exist_ok=True)
    rows_sorted = sorted(rows, key=lambda r: r.get("term_id", ""))
    with TERMS_FILE.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TERMS_FIELDS)
        writer.writeheader()
        for row in rows_sorted:
            writer.writerow({k: row.get(k, "") for k in TERMS_FIELDS})


def _next_term_id(rows: list[dict]) -> str:
    """Compute the next T0001-style term id.

    :param rows: existing glossary rows.
    :return: id one above the highest existing numeric id.
    """
    max_id = 0
    for row in rows:
        tid = row.get("term_id", "")
        if tid.startswith("T") and tid[1:].isdigit():
            max_id = max(max_id, int(tid[1:]))
    return f"T{max_id + 1:04d}"


def lookup(term: str, source_lang: str, target_lang: str,
           rows: list[dict] | None = None) -> str | None:
    """Find the target-language value for a term, matching the source column case-insensitively.

    :param term: source-language term.
    :param source_lang: language code of the term.
    :param target_lang: language code of the wanted value.
    :param rows: glossary rows; None loads them from disk.
    :return: the target value, or None when the term is unknown or its target column is empty.
    """
    if rows is None:
        rows = load_glossary()
    term_lower = term.strip().lower()
    for row in rows:
        val = (row.get(source_lang) or "").strip()
        if val.lower() == term_lower:
            target = (row.get(target_lang) or "").strip()
            return target if target else None
    return None


def add_term(translations: dict[str, str], domain: str = "",
             notes: str = "", rows: list[dict] | None = None) -> str:
    """Add a term row and save the glossary.

    :param translations: language code to term, e.g. {"en": "screw", "fr": "vis"}.
    :param domain: domain label.
    :param notes: free-text notes.
    :param rows: glossary rows to extend; None loads them from disk.
    :return: the assigned term id.
    """
    if rows is None:
        rows = load_glossary()
    tid = _next_term_id(rows)
    row = {"term_id": tid, "domain": domain, "notes": notes,
           "added_date": date.today().isoformat()}
    for lang in LANGUAGES:
        row[lang] = translations.get(lang, "")
    rows.append(row)
    save_glossary(rows)
    return tid


def merge_terms(term_pairs: list[dict], source_lang: str,
                target_lang: str) -> int:
    """Bulk-add terms, skipping those whose source-language value already exists.

    :param term_pairs: dicts keyed by language code (at least source_lang and target_lang), with optional "domain" and "notes".
    :param source_lang: language code used to detect duplicates.
    :param target_lang: target language code.
    :return: number of terms added.
    """
    rows = load_glossary()
    existing = {(row.get(source_lang) or "").strip().lower() for row in rows}
    added = 0
    for pair in term_pairs:
        src_val = (pair.get(source_lang) or "").strip()
        if not src_val or src_val.lower() in existing:
            continue
        tid = _next_term_id(rows)
        row = {"term_id": tid, "domain": pair.get("domain", ""),
               "notes": pair.get("notes", ""),
               "added_date": date.today().isoformat()}
        for lang in LANGUAGES:
            row[lang] = pair.get(lang, "")
        rows.append(row)
        existing.add(src_val.lower())
        added += 1
    if added:
        save_glossary(rows)
    return added


# ---------------------------------------------------------------------------
# Pattern-based DNT
# ---------------------------------------------------------------------------

DNT_MATCH_MODES = ("exact", "prefix", "glob", "regex")


def dnt_rule_problem(pattern: str, match_mode: str) -> str | None:
    """Explain why a DNT rule must not be saved.

    A rule matching every segment would leave the whole document untranslated, and an invalid regex is skipped by DntMatcher silently.

    :param pattern: rule pattern.
    :param match_mode: one of DNT_MATCH_MODES.
    :return: the reason, or None when the rule is acceptable.
    """
    pattern = (pattern or "").strip()
    if not pattern:
        return "The pattern cannot be empty"
    if match_mode not in DNT_MATCH_MODES:
        return f"match_mode must be one of {', '.join(DNT_MATCH_MODES)}"
    if match_mode in ("prefix", "glob") and not pattern.strip("*?[]"):
        return "A pattern made only of wildcards would match every segment"
    if match_mode == "regex":
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            return f"Invalid regular expression: {e}"
        if compiled.fullmatch("") or compiled.fullmatch("Do not translate this ordinary sentence"):
            return "This expression would match ordinary text, not just codes"
    return None


class DntMatcher:
    """Compiled matcher for pattern-based DNT rules."""

    def __init__(self, rules: list[dict]) -> None:
        """Compile DNT rules.

        :param rules: rule dicts with "pattern" and "match_mode" (default "exact"); empty patterns and invalid regexes are ignored.
        """
        self._exact: set[str] = set()
        self._prefixes: list[str] = []
        self._globs: list[str] = []
        self._regexes: list[re.Pattern] = []
        for rule in rules:
            pattern = rule.get("pattern", "").strip()
            mode = rule.get("match_mode", "exact").strip()
            if not pattern:
                continue
            if mode == "exact":
                self._exact.add(pattern)
            elif mode == "prefix":
                self._prefixes.append(pattern.rstrip("*"))
            elif mode == "glob":
                self._globs.append(pattern)
            elif mode == "regex":
                try:
                    self._regexes.append(re.compile(pattern))
                except re.error:
                    pass

    def is_dnt(self, token: str) -> bool:
        """Check a token against all DNT rules.

        :param token: token to test.
        :return: True when any rule matches.
        """
        if token in self._exact:
            return True
        for prefix in self._prefixes:
            if token.startswith(prefix):
                return True
        for glob_pat in self._globs:
            if fnmatch.fnmatch(token, glob_pat):
                return True
        for regex in self._regexes:
            if regex.fullmatch(token):
                return True
        return False


def load_dnt_rules() -> list[dict]:
    """Load DNT rules from dnt.csv.

    :return: one dict per row; empty when the file does not exist.
    """
    if not DNT_FILE.exists():
        return []
    rows = []
    with DNT_FILE.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def load_dnt_matcher() -> DntMatcher:
    """Load DNT rules and compile them.

    :return: a matcher over the rules in dnt.csv.
    """
    return DntMatcher(load_dnt_rules())


def save_dnt_rules(rules: list[dict]) -> None:
    """Save DNT rules to dnt.csv.

    :param rules: rule dicts; keys outside the DNT columns are dropped.
    """
    GLOSSARY_DIR.mkdir(parents=True, exist_ok=True)
    with DNT_FILE.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DNT_FIELDS)
        writer.writeheader()
        for row in rules:
            writer.writerow({k: row.get(k, "") for k in DNT_FIELDS})


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("Glossary dir: %s", GLOSSARY_DIR)
    logger.info("Terms file exists: %s", TERMS_FILE.exists())
    logger.info("DNT file exists: %s", DNT_FILE.exists())
    rows = load_glossary()
    logger.info("Glossary terms loaded: %d", len(rows))
    rules = load_dnt_rules()
    logger.info("DNT rules loaded: %d", len(rules))
    if rules:
        m = load_dnt_matcher()
        test_tokens = ["NAS6404A22", "MS21299C4", "IF2.03.08", "PR1440MB2",
                        "hello", "vis", "LATECOERE", "12/16/2024"]
        logger.info("DNT matching test:")
        for t in test_tokens:
            logger.info("  %-20s -> %s", t, "DNT" if m.is_dnt(t) else "translate")
