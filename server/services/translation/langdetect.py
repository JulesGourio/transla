"""
langdetect.py - Stdlib-only language detector for {EN, FR, ES, DE, CS, BG, PT, AR}.

Three detection tiers:
  1. Script-based   - Cyrillic block -> Bulgarian, Arabic block -> Arabic
     (the only non-Latin scripts in scope).
  2. Diacritic-based - unique characters per language (e.g. 'r' for CS, 'l' for PL,
     'ss' for DE, 'oe' for FR, 'n' for ES, 'ã/õ' for PT) plus shared-diacritic scoring.
  3. Keyword-based  - for ASCII-heavy text (FR/EN/ES/DE/PT) we score against
     short stopword lists.

Returns (iso_code, confidence) where confidence is in [0.0, 1.0].

Also exports DNT (do-not-translate) helpers used by audit.py:
    is_dnt_candidate(text)
    extract_dnt_tokens(text)
"""

from __future__ import annotations
import logging
import os
import re
import unicodedata
from collections import Counter
from functools import lru_cache

from .glossary_io import DntMatcher

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Language profiles
# ---------------------------------------------------------------------------

LANG_PROFILES = {
    "cs": {
        "unique": set("\u0159\u016f"),                  # r-hacek, u-ring (Czech-only among scope)
        "common": set("\u011b\u017e\u0161\u010d\u010f\u0165\u0148\u00fd\u00e1\u00ed\u00fa"),
        "keywords": ["je", "na", "se", "pro", "dle", "ne", "po", "do", "od", "to", "byl", "jsou", "toto"],
    },
    "bg": {
        # Cyrillic-script language; diacritics not used here, script test instead
        "unique": set(),
        "common": set(),
        "keywords": [],
    },
    "de": {
        "unique": set("\u00df"),                       # eszett (German-only)
        "common": set("\u00e4\u00f6\u00fc\u00c4\u00d6\u00dc"),
        "keywords": ["der", "die", "das", "und", "mit", "von", "ein", "ist", "den", "im",
                     "nicht", "auch", "auf", "sind", "werden", "wird"],
    },
    "fr": {
        "unique": set("\u0153\u0152"),                 # ligature oe
        "common": set("\u00e9\u00e8\u00ea\u00eb\u00e0\u00e2\u00f9\u00fb\u00e7\u00ef\u00ee\u00f4"),
        # "en"/"la"/"de" are also Spanish words; French must list them too or it cedes
        # every sentence containing them to Spanish.
        "keywords": ["le", "la", "les", "de", "du", "des", "et", "un", "une", "pour",
                     "en", "sur", "avec", "dans", "que", "qui", "ce", "cette", "ces",
                     "au", "aux", "vous", "nous", "pas", "sont", "est", "\u00eatre", "avoir"],
    },
    "es": {
        "unique": set("\u00f1\u00d1\u00bf\u00a1"),     # n-tilde, inverted punctuation
        "common": set("\u00e1\u00e9\u00ed\u00f3\u00fa\u00c1\u00c9\u00cd\u00d3\u00da"),
        "keywords": ["el", "la", "los", "las", "de", "del", "y", "en", "es", "para",
                     "con", "por", "un", "una", "que", "no", "se"],
    },
    "en": {
        "unique": set(),
        "common": set(),
        "keywords": ["the", "of", "and", "is", "to", "for", "with", "in", "on", "per",
                     "are", "this", "that", "not", "shall", "must"],
    },
    "pt": {
        "unique": set("ãõÃÕ"),     # a-tilde, o-tilde (nasal vowels, PT-only among scope)
        "common": set("áéíóúâêôàçÁÉÍÓÚÂÊÔÀÇ"),
        "keywords": ["o", "a", "os", "as", "de", "do", "da", "e", "em", "para",
                     "com", "por", "um", "uma", "que", "não", "se", "são", "está"],
    },
    "ar": {
        # Arabic script; detected by script test in detect_language's tier 1, like bg.
        "unique": set(),
        "common": set(),
        "keywords": [],
    },
}


SUPPORTED_LANGS = list(LANG_PROFILES.keys())

# Company name whose accented letters must not count as language signal: it would
# make an otherwise English sentence read as French. Matches any accent
# combination on the two e's, case-insensitively.
_PROPER_NOUN_PATTERN = re.compile(r"lat[eé]co[eè]re", re.IGNORECASE)


def _strip_diacritic_bias_tokens(text: str) -> str:
    """Remove known proper nouns before diacritic-tier scoring; their accents carry no language signal.

    :param text: text to clean.
    :return: the text with proper nouns replaced by spaces.
    """
    return _PROPER_NOUN_PATTERN.sub(" ", text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_language(text: str, default_lang: str | None = None) -> tuple[str, float]:
    """Detect the language of a text with the stdlib tiers (script, unique diacritics, shared diacritics, keywords).

    :param text: text to analyze.
    :param default_lang: the document's dominant language; returned with low confidence when the text carries no distinguishing signal. None means such text yields "??".
    :return: (ISO code, confidence in [0, 1]); ("??", 0.0) for empty or unanalyzable text.
    """
    if not text or not text.strip():
        return "??", 0.0

    # Lower-cased letters only, so diacritic matching ignores capitalization ("PREMIÈRE ÉDITION").
    letters = [c.lower() for c in _strip_diacritic_bias_tokens(text) if c.isalpha()]
    if not letters:
        return (default_lang, 0.4) if default_lang else ("??", 0.0)
    letter_count = len(letters)

    # ---- Tier 1: script-based ----
    # Cyrillic is the only Cyrillic-script language in scope, so any non-trivial presence
    # means Bulgarian, even when a longer Latin echo of the sentence shares the segment.
    # Gating on count and ratio separates real BG text from a stray look-alike letter in a code.
    cyrillic = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    cyrillic_ratio = cyrillic / letter_count
    if cyrillic >= 2 and cyrillic_ratio > 0.08:
        return "bg", min(0.99, 0.75 + cyrillic_ratio * 0.24)

    # Same reasoning for Arabic, the only Arabic-script language in scope.
    arabic = sum(1 for c in letters if "\u0600" <= c <= "\u06ff")
    arabic_ratio = arabic / letter_count
    if arabic >= 2 and arabic_ratio > 0.08:
        return "ar", min(0.99, 0.75 + arabic_ratio * 0.24)

    # ---- Tier 2: unique-diacritic detection ----
    # Strong signals first - one unique char => high confidence
    unique_hits = {lang: sum(1 for c in letters if c in profile["unique"])
                   for lang, profile in LANG_PROFILES.items()
                   if profile["unique"]}
    strong = [(lang, hits) for lang, hits in unique_hits.items() if hits > 0]
    if strong:
        # Pick the language with the most unique-char hits
        strong.sort(key=lambda kv: -kv[1])
        winner, hits = strong[0]
        confidence = min(0.98, 0.80 + 0.04 * hits)
        return winner, confidence

    # ---- Tier 2b: shared-diacritic scoring ----
    # Gated on hit count OR share: a short sentence followed by its longer EN echo dilutes the share.
    diacritic_hits = {}
    for lang, profile in LANG_PROFILES.items():
        if lang in ("en", "bg"):
            continue
        common = profile["common"]
        if not common:
            continue
        diacritic_hits[lang] = sum(1 for c in letters if c in common)

    max_hits = max(diacritic_hits.values(), default=0)
    # A tie means the matched characters do not distinguish (e.g. 'é' is common to CS/FR/ES);
    # defer to keyword scoring instead of picking by dict order.
    tied = [lang for lang, hits in diacritic_hits.items() if hits == max_hits and hits > 0]
    if len(tied) == 1 and max_hits > 0:
        best_lang = tied[0]
        ratio = max_hits / letter_count
        if max_hits >= 2 or ratio > 0.02:
            signal = max(ratio, min(max_hits, 10) / 40)
            confidence = min(0.88, 0.55 + signal * 4)
            return best_lang, confidence

    # ---- Tier 3: keyword scoring (ASCII text) ----
    return _keyword_score(text, default_lang=default_lang)


def _keyword_score(text: str, default_lang: str | None = None) -> tuple[str, float]:
    """Score a text against per-language stopword lists.

    :param text: text to analyze.
    :param default_lang: returned with low confidence when there is no signal (no words, no stopword hit, or a tie); None means "??".
    :return: (ISO code, confidence) of the best-scoring language.
    """
    words = re.findall(r"[A-Za-z]+", text.lower())
    if not words:
        return (default_lang, 0.4) if default_lang else ("??", 0.0)
    word_set = Counter(words)
    total = sum(word_set.values())

    scores = {}
    for lang, profile in LANG_PROFILES.items():
        if not profile["keywords"]:
            continue
        hits = sum(word_set[k] for k in profile["keywords"])
        scores[lang] = hits / total if total else 0

    if not scores:
        return (default_lang, 0.4) if default_lang else ("??", 0.0)
    max_score = max(scores.values())
    if max_score == 0:
        # No stopwords matched at all — no basis to guess a language.
        return (default_lang, 0.45) if default_lang else ("??", 0.0)
    tied = [lang for lang, score in scores.items() if score == max_score]
    if len(tied) > 1:
        # A genuine tie is no signal, same as in tier 2b.
        return (default_lang, 0.45) if default_lang else ("??", 0.0)
    best_lang = tied[0]
    confidence = min(0.85, 0.45 + max_score * 3)
    return best_lang, confidence


def detect_language_pair(text_a: str, text_b: str) -> tuple[tuple[str, float], tuple[str, float]]:
    """Detect both texts of a candidate bilingual pair.

    :param text_a: first text.
    :param text_b: second text.
    :return: ((lang, confidence) of text_a, (lang, confidence) of text_b).
    """
    return detect_language(text_a), detect_language(text_b)


# ---------------------------------------------------------------------------
# Span-level detection (short halves of a candidate bilingual split)
# ---------------------------------------------------------------------------

# Vendored model shared with the chat translation bridge.
_FASTTEXT_MODEL_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "lid.176.ftz",
)


@lru_cache(maxsize=1)
def _fasttext_model():
    """Load the fastText lid.176 model once.

    :return: the model, or None when the package or the model file is unavailable.
    """
    try:
        import fasttext
        fasttext.FastText.eprint = lambda *_a, **_kw: None
        return fasttext.load_model(_FASTTEXT_MODEL_PATH)
    except Exception:
        return None


def _fasttext_scores(text: str, langs) -> dict[str, float] | None:
    """Compute fastText lid.176 probabilities renormalized over a subset of languages.

    Renormalizing over the in-scope languages sends noise mass to out-of-scope languages instead of a wrong in-scope one. The text is lower-cased first because the model misreads ALL-CAPS accented titles.

    :param text: text to analyze.
    :param langs: language codes to keep.
    :return: {code: probability} summing to 1.0, or None when the model is unavailable or none of the languages got any mass.
    """
    model = _fasttext_model()
    if model is None:
        return None
    single_line = " ".join(text.split()).lower() + "\n"
    try:
        # Direct binding call: the Python .predict() wrapper breaks under NumPy>=2.0.
        preds = model.f.predict(single_line, 176, 0.0, "strict")
    except Exception:
        return None
    langs = set(langs)
    scores: dict[str, float] = {}
    for prob, label in preds:
        code = label.replace("__label__", "")
        if code in langs:
            scores[code] = scores.get(code, 0.0) + prob
    total = sum(scores.values())
    if total <= 0:
        return None
    return {k: v / total for k, v in scores.items()}


def _fasttext_restricted(text: str) -> tuple[str, float] | None:
    """Predict with fastText restricted to the supported languages.

    :param text: text to analyze.
    :return: (best language, renormalized probability), or None when fastText has no verdict.
    """
    scores = _fasttext_scores(text, LANG_PROFILES.keys())
    if not scores:
        return None
    best = max(scores, key=lambda k: scores[k])
    return best, scores[best]


def detect_language_constrained(
    text: str, candidates, default_lang: str | None = None, source_lang: str | None = None,
) -> tuple[str, float]:
    """Ensemble detector restricted to a small candidate set; the primary detector for mono segments.

    Combines the stdlib heuristic with fastText scores renormalized over the candidates. Only an agreement of both detectors reports high confidence (>= 0.85); a single detector is capped at 0.80 so a confidently wrong call on a short cognate title is never kept silently untranslated. A single word without agreement is biased to the source language.

    :param text: text to analyze.
    :param candidates: language codes the result may take, normally the job's {source, target} pair plus languages found in the document.
    :param default_lang: the document's dominant language, used when there is no usable signal; None yields "??".
    :param source_lang: the job's source language, used for the single-word bias; None disables it.
    :return: (ISO code, confidence); ("??", 0.0) for empty text or no usable signal.
    """
    candidates = set(candidates)
    if not text or not text.strip():
        return "??", 0.0

    # Cyrillic presence is an unambiguous Bulgarian signal and must win over a wrong
    # fastText call on the Latin remainder of a mixed cell.
    if "bg" in candidates:
        letters = [c for c in text if c.isalpha()]
        cyr = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
        if cyr >= 2 and letters and cyr / len(letters) > 0.08:
            return "bg", min(0.99, 0.85 + (cyr / len(letters)) * 0.14)

    # Same for Arabic script.
    if "ar" in candidates:
        letters = [c for c in text if c.isalpha()]
        arb = sum(1 for c in letters if "؀" <= c <= "ۿ")
        if arb >= 2 and letters and arb / len(letters) > 0.08:
            return "ar", min(0.99, 0.85 + (arb / len(letters)) * 0.14)

    h_lang, h_conf = detect_language(text, default_lang=default_lang)
    if h_lang not in candidates:
        h_lang, h_conf = "??", 0.0

    ft = _fasttext_scores(text, candidates)
    if ft:
        ft_lang = max(ft, key=lambda k: ft[k])
        ft_conf = ft[ft_lang]
    else:
        ft_lang, ft_conf = "??", 0.0

    # Agreement of two independent detectors is the strongest signal available.
    if h_lang != "??" and h_lang == ft_lang:
        return h_lang, min(0.99, max(0.88, ft_conf, h_conf))

    # fastText is confidently wrong on bare single words, so bias to the source language:
    # a redundant translation is caught on review, a silently skipped one is not.
    if len(text.split()) <= 1 and source_lang and source_lang in candidates:
        return source_lang, 0.5

    # Disagreement or a single detector: trust the confident one, capped below the keep bar.
    if ft_lang != "??" and ft_conf >= 0.70:
        return ft_lang, min(0.80, ft_conf)
    if h_lang != "??" and h_conf >= 0.55:
        return h_lang, min(0.80, h_conf)
    if ft_lang != "??" and ft_conf >= 0.55:
        return ft_lang, min(0.80, ft_conf)

    # No usable signal within the candidate pair.
    return (default_lang, 0.5) if default_lang else ("??", 0.0)


def detect_span_language(text: str) -> tuple[str, float]:
    """Detect the language of a short span, with a fastText second opinion for weak verdicts.

    Meant for the short halves produced by the inline pre-split heuristics. Agreement raises confidence, a confident disagreement wins, anything else keeps the heuristic verdict.

    :param text: span text.
    :return: (ISO code, confidence).
    """
    lang, conf = detect_language(text)
    if conf >= 0.60:
        return lang, conf
    ft = _fasttext_restricted(text)
    if ft is None:
        return lang, conf
    ft_lang, ft_conf = ft
    if ft_lang == lang:
        return lang, max(conf, ft_conf)
    if ft_conf >= 0.60:
        return ft_lang, ft_conf
    return lang, conf


# ---------------------------------------------------------------------------
# DNT (do-not-translate) detection
# ---------------------------------------------------------------------------

# Patterns observed in LATECOERE sample docs.
DNT_PATTERNS = [
    # Aerospace fastener part numbers: NAS6404A22, MS21299C4, NAS1726-4D
    r"\b[A-Z]{2,5}\d{3,6}[A-Z]?\d*[-\d]*\b",
    # Internal references: IF2.03.08, F46C521A0, D5211535000901
    r"\b[A-Z]{1,3}\d+(?:[.-]\d+)+[A-Z]?\d*\b",
    # Long alpha-numeric part numbers: F46C521124037
    r"\b[A-Z]\d{2}[A-Z]\d{3}\d{6}\b",
    # PR/EN/MS sealants and standards: PR1440MB2, EN9100, AS9100
    r"\b(?:PR|EN|MS|AS|ISO|ASNA|NSA)\d{3,5}[A-Z]{0,3}\d*\b",
    # Date formats: 12/16/2024, 22/04/2024, 08/12/2023
    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
    # ISO dates: 2024-04-22
    r"\b\d{4}-\d{2}-\d{2}\b",
    # Digit-first internal references, e.g. 252A90008200
    r"\b\d{2,}[A-Z]{1,3}\d{3,}\b",
]

DNT_REGEX = re.compile("|".join(DNT_PATTERNS))


# extract.py reads <w:noBreakHyphen/> as U+2011; the part-number patterns above
# are written with the ASCII hyphen.
_NBH_TO_HYPHEN = str.maketrans("\u2011", "-")


def normalize_hyphens(text: str) -> str:
    """Replace non-breaking hyphens (U+2011) with ASCII hyphens so part-number patterns match.

    :param text: text to normalize.
    :return: the normalized text.
    """
    return text.translate(_NBH_TO_HYPHEN)


def extract_dnt_tokens(text: str, matcher: DntMatcher | None = None) -> list[str]:
    """List the do-not-translate tokens of a text: built-in regex matches (part numbers, standards, dates) plus whitespace-delimited tokens matching a reviewer-curated rule.

    :param text: text to scan.
    :param matcher: reviewer-curated DNT rules; None means built-in patterns only.
    :return: the tokens found.
    """
    text = normalize_hyphens(text)
    tokens = DNT_REGEX.findall(text)
    if matcher is not None:
        for word in re.findall(r"\S+", text):
            stripped = word.strip(".,;:()[]")
            if stripped and stripped not in tokens and matcher.is_dnt(stripped):
                tokens.append(stripped)
    return tokens


def is_dnt_candidate(text: str, matcher: DntMatcher | None = None) -> bool:
    """Tell whether a whole text is do-not-translate: a DNT token, a single uppercase revision letter, a pure number or date, a hyphenated or apostrophed uppercase name, or a match of a curated rule.

    :param text: text to test.
    :param matcher: reviewer-curated DNT rules; None means built-in patterns only.
    :return: True when the text must skip translation.
    """
    s = normalize_hyphens(text).strip()
    if not s:
        return False
    # Revision indicator
    if len(s) == 1 and s.isupper() and s.isalpha():
        return True
    if re.fullmatch(r"[\d.,\-+]+", s):
        return True
    if DNT_REGEX.fullmatch(s):
        return True
    if re.fullmatch(r"[A-Z][A-Z\-']+", s) and len(s) >= 4:
        # Could be an acronym or heading; only a hyphen or apostrophe (surnames) makes it DNT.
        if "-" in s or "'" in s:
            return True
    if matcher is not None and matcher.is_dnt(s):
        return True
    return False


def is_numeric_only(text: str) -> bool:
    """Tell whether a text contains no letters.

    :param text: text to test.
    :return: True for numbers, punctuation and dates only.
    """
    return not any(c.isalpha() for c in text)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    samples = [
        ("en", "Install the screw and washer per the assembly drawing."),
        ("fr", "Installer la vis et la rondelle selon le plan d'assemblage."),
        ("es", "Instalar el tornillo y la arandela segun el plano de montaje."),
        ("de", "Schraube und Unterlegscheibe gem\u00e4\u00df Montagezeichnung einbauen."),
        ("cs", "Mont\u00e1\u017e mechanismu dve\u0159\u00ed \u2013 1. \u010d\u00e1st"),
        ("bg", "\u0417\u0430\u0432\u044a\u0440\u0448\u0432\u0430\u043d\u0435 \u043d\u0430 \u0432\u0440\u0430\u0442\u0430"),
        ("bg", "\u041e\u0411\u041e\u0420\u0423\u0414\u0412\u0410\u041d\u0415 ARD"),
        ("fr", "PREMI\u00c8RE \u00c9DITION"),
        # French without accents or stopword overlap: no signal.
        ("??", "OPERATION 20 PERCAGE"),
    ]

    logger.info("=== detect_language self-test (no document bias) ===")
    correct = 0
    for expected, text in samples:
        got, conf = detect_language(text)
        ok = "OK" if got == expected else "FAIL"
        if got == expected:
            correct += 1
        logger.info("  %s  expected=%s  got=%s(%.2f)  text=%r", ok, expected, got, conf, text[:50])
    logger.info("%d/%d correct", correct, len(samples))

    logger.info("=== detect_language self-test (with document-dominant-language bias) ===")
    biased_samples = [
        # Zero-signal segments resolved by the document-dominant-language bias.
        ("cs", "Utahovac\u00ed moment 10,2 N.m", "cs"),
        ("en", "DOOR FINISHING STEP 2", "en"),
        ("fr", "OPERATION 20 PERCAGE", "fr"),
    ]
    correct = 0
    for expected, text, bias in biased_samples:
        got, conf = detect_language(text, default_lang=bias)
        ok = "OK" if got == expected else "FAIL"
        if got == expected:
            correct += 1
        logger.info("  %s  expected=%s  got=%s(%.2f)  bias=%s  text=%r", ok, expected, got, conf, bias, text[:50])
    logger.info("%d/%d correct", correct, len(biased_samples))

    logger.info("=== detect_language_constrained self-test (candidates={fr,en}) ===")
    # Short French technical segments must resolve to fr or stay below 0.85, never a silent en keep.
    # Requires the fastText model.
    constrained_samples = [
        ("fr", "Percer les trous selon plan"),
        ("fr", "Rédacteur"),
        ("fr", "Vérification dimensionnelle"),
        ("fr", "Contrôle visuel de la pièce"),
        # Cognate title: must not come back en >= 0.85.
        ("keep<0.85", "OPERATION 20 PERCAGE"),
        # Genuine English boilerplate: en with high confidence.
        ("en", "This document is the property of LATECOERE, it may not be communicated."),
    ]
    for expected, text in constrained_samples:
        got, conf = detect_language_constrained(text, {"fr", "en"}, default_lang="fr")
        if expected == "keep<0.85":
            ok = "OK" if not (got == "en" and conf >= 0.85) else "FAIL"
        else:
            ok = "OK" if got == expected else "FAIL"
        logger.info("  %s  expected=%s  got=%s(%.2f)  text=%r", ok, expected, got, conf, text[:50])

    logger.info("=== DNT extraction self-test ===")
    dnt_samples = [
        "Install NAS6404A22 screw and MS21299C4 washer per IF2.03.08.",
        "Apply PR1440MB2 sealant.",
        "Part number F46C521A0 dated 22/04/2024.",
        "ISO 9001 quality standard.",
    ]
    for s in dnt_samples:
        tokens = extract_dnt_tokens(s)
        logger.info("  %s\n    -> %s", s, tokens)

    logger.info("=== is_dnt_candidate self-test ===")
    cands = ["A", "BERNARD-SENTAURENS", "NAS6404A22", "12/16/2024", "10,2", "Door"]
    for c in cands:
        logger.info("  %-30r -> %s", c, is_dnt_candidate(c))
