"""app/parsing/normalize.py
Text normalization for consistent hashing and comparison.
"""

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)

# Non-breaking hyphen U+2011, various dash/hyphen characters
HYPHEN_CHARS = "\u2010\u2011\u2012\u2013\u2014\u2015\ufe58\ufe63\uff0d"
# Various quote characters
QUOTE_CHARS = {
    "\u2018": "'",
    "\u2019": "'",  # single curly quotes
    "\u201c": '"',
    "\u201d": '"',  # double curly quotes
    "\u00ab": '"',
    "\u00bb": '"',  # guillemets
    "\u2039": "'",
    "\u203a": "'",  # single guillemets
}

# Pattern to match section references like "see 2.1, 4.3" or "Section 4.2"
CROSS_REF_PATTERN = re.compile(
    r"(?:(?:see|See|SEE)\s+(?:\d+(?:\.\d+)*(?:,\s*)?)+)|"
    r"(?:(?:Section|section|SECTION)\s+\d+(?:\.\d+)*)"
)

# Pattern to extract individual section numbers from cross-references
SECTION_NUM_PATTERN = re.compile(r"\d+(?:\.\d+)*")


def normalize_text(text: str) -> str:
    """Normalize text for consistent hashing and comparison.

    - Replace all Unicode hyphens/dashes with ASCII hyphen
    - Normalize quotes
    - Collapse whitespace (including newlines)
    - Strip leading/trailing whitespace
    - NFC normalization
    """
    # NFC normalization first
    result = unicodedata.normalize("NFC", text)

    # Replace Unicode hyphens with ASCII hyphen
    for ch in HYPHEN_CHARS:
        result = result.replace(ch, "-")

    # Replace curly quotes
    for old, new in QUOTE_CHARS.items():
        result = result.replace(old, new)

    # Collapse whitespace
    result = re.sub(r"\s+", " ", result)

    return result.strip()


def normalize_for_hash(text: str) -> str:
    """More aggressive normalization for hashing.

    Same as normalize_text plus:
    - Lowercase
    - Remove all punctuation-level differences that shouldn't affect hash
    """
    result = normalize_text(text)
    return result.lower()


def rejoin_page_break_text(spans_text: list[str]) -> str:
    """Rejoin text that was split across page breaks.

    Handles cases where words are split at page boundaries.
    """
    if not spans_text:
        return ""

    result = spans_text[0]
    for i in range(1, len(spans_text)):
        current = spans_text[i]
        if not current:
            continue

        # If previous text ends with a letter and current starts with lowercase,
        # it's likely a word split across pages - join without space
        if result and result[-1].isalpha() and current[0].islower() and not result.endswith(" "):
            result += current
        elif result and not result.endswith(" ") and not current.startswith(" "):
            result += " " + current
        else:
            result += current

    return result


def extract_cross_refs(text: str) -> list[str]:
    """Extract cross-reference section numbers from text.

    E.g., "see 2.1, 4.3" → ["2.1", "4.3"]
          "Section 4.2" → ["4.2"]
    """
    refs: list[str] = []
    for match in CROSS_REF_PATTERN.finditer(text):
        ref_text = match.group()
        for num_match in SECTION_NUM_PATTERN.finditer(ref_text):
            refs.append(num_match.group())
    return refs


def extract_numeric_tokens(text: str) -> list[dict]:
    """Extract numeric values with optional units from text.

    Returns list of dicts with 'value', 'unit', 'raw' keys.
    """
    # Match numbers (possibly with decimals) optionally followed by a unit
    pattern = re.compile(
        r"(\d+(?:\.\d+)?)\s*"
        r"(mmHg|bpm|cm|mm|%|seconds?|minutes?|hours?|years?|days?|"
        r"readings?|cycles?|measurements?|s\b|V\b)?"
    )

    tokens = []
    for match in pattern.finditer(text):
        value_str = match.group(1)
        unit = match.group(2) or ""
        tokens.append(
            {
                "value": float(value_str) if "." in value_str else int(value_str),
                "unit": unit,
                "raw": match.group(0).strip(),
            }
        )

    return tokens
