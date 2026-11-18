"""Citation verifier (BUILD_PLAN section 5). Pure: no I/O, no LLM.

A source is valid only when its locator exists in the evidence item and its excerpt fuzzy-matches
the stored text of that locator. This is the check that catches invented quotes.

Two rules keep the fuzzy match honest:
- An excerpt longer than the page is compared with the whole page (`ratio`), so a quote that
  contains the page plus invented words does not pass.
- A passing citation returns the text span found in the stored page (`matched`), not the
  model's string, so what is saved as a quote is always text that exists in the evidence."""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from rapidfuzz import fuzz

MIN_SCORE = 90.0
MIN_EXCERPT_CHARS = 8  # a shorter excerpt matches almost any page, so it proves nothing
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None  # plain words, shown to the model and logged
    score: float = 0.0
    matched: str = ""  # the span of the stored page that the excerpt matched (ok only)
    locator: str = ""  # the page's own locator, as stored (ok only)


def normalize(text: str) -> str:
    """Case, whitespace and the `&lt;` escaping of evidence blocks do not count as a difference."""
    return _WHITESPACE.sub(" ", text.replace("&lt;", "<")).strip().casefold()


def _fold(text: str) -> tuple[str, list[int]]:
    """Casefolded, whitespace-collapsed text, and for each output character the index it came
    from in `text`, so a matched span can be cut out of the original."""
    out: list[str] = []
    origin: list[int] = []
    gap = True  # drops leading whitespace
    for i, ch in enumerate(text):
        if ch.isspace():
            if not gap:
                out.append(" ")
                origin.append(i)
                gap = True
            continue
        gap = False
        for folded in ch.casefold():
            out.append(folded)
            origin.append(i)
    if out and out[-1] == " ":
        out.pop()
        origin.pop()
    return "".join(out), origin


def _find_page(locator: str, pages: Mapping[str, str]) -> tuple[str, str] | None:
    wanted = normalize(locator)
    for key, text in pages.items():
        if normalize(key) == wanted:
            return key, text
    return None


def verify_citation(excerpt: str, locator: str, pages: Mapping[str, str]) -> Verdict:
    """`pages` maps each locator of one evidence item to its stored text."""
    found = _find_page(locator, pages)
    if found is None:
        return Verdict(False, "citation rejected: locator not found in the evidence")
    key, text = found
    needle = normalize(excerpt)
    if len(needle) < MIN_EXCERPT_CHARS:
        return Verdict(False, "citation rejected: excerpt too short to verify")
    hay, origin = _fold(text)
    if not hay:
        return Verdict(False, "citation rejected: excerpt not found in the evidence")
    if len(needle) > len(hay):
        score = float(fuzz.ratio(needle, hay))
        start, end = 0, len(hay)
    else:
        alignment = fuzz.partial_ratio_alignment(needle, hay)
        score = float(alignment.score) if alignment else 0.0
        start, end = (alignment.dest_start, alignment.dest_end) if alignment else (0, 0)
    if score < MIN_SCORE or end <= start:
        return Verdict(False, "citation rejected: excerpt not found in the evidence", score)
    matched = text[origin[start] : origin[end - 1] + 1].strip()
    return Verdict(True, None, score, matched=matched, locator=key)
