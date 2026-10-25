"""Small ASD-STE100 checks and grounding checks for generated text."""

from __future__ import annotations

import re

MAX_DESCRIPTIVE_WORDS = 25
MAX_PROCEDURAL_WORDS = 20
_NUMBER = re.compile(r"(?<![A-Za-z])[-+]?\d[\d,]*(?:\.\d+)?")
_SPECULATIVE = re.compile(
    r"\b(skyrocket|moon|explode|guarantee[ds]?|certainly|definitely|surely|huge gains|can't lose)\b",
    re.IGNORECASE,
)


def sentences(text: str) -> list[str]:
    cleaned = re.sub(r"\s+", " ", text or "").strip()
    if not cleaned:
        return []
    # Do not split on decimal points such as 4.5 or abbreviations such as U.S.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"(])", cleaned)
    return [part.strip() for part in parts if part.strip()]


def word_count(sentence: str) -> int:
    return len(re.findall(r"[A-Za-z0-9$%.,+\-/]+", sentence))


def issues(text: str, max_words: int = MAX_DESCRIPTIVE_WORDS) -> list[str]:
    """Return the ASD-STE100 problems found in the text (empty = compliant)."""
    found: list[str] = []
    for sentence in sentences(text):
        count = word_count(sentence)
        if count > max_words:
            found.append(f"{count} words (max {max_words}): {sentence}")
    if _SPECULATIVE.search(text or ""):
        found.append("Speculative wording.")
    if re.search(r"https?://", text or "", re.IGNORECASE):
        found.append("Contains a URL.")
    return found


def _numbers(text: str) -> list[float]:
    values = []
    for raw in _NUMBER.findall(text or ""):
        try:
            values.append(float(raw.replace(",", "").lstrip("+")))
        except ValueError:
            continue
    return values


def numbers_grounded(text: str, source_text: str) -> bool:
    """True when every number in text also appears in source_text.

    Small whole numbers (0-10) are always allowed because sentences use them for
    counts ("2 sources", "5 sessions"). Rounding of a source value is allowed.
    """
    source = _numbers(source_text)
    for value in _numbers(text):
        if value.is_integer() and 0 <= abs(value) <= 10:
            continue
        if not any(
            abs(value - item) < 0.006
            or abs(value - round(item, 1)) < 0.006
            or abs(value - round(item)) < 0.006
            or abs(abs(value) - abs(item)) < 0.006
            for item in source
        ):
            return False
    return True
