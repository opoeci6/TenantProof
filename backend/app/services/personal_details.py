"""Personal details and the placeholders the model sees instead (issue #60). Pure: no database,
no model calls.

What is hidden is who and where: people's names, street addresses and unit numbers, phone
numbers, email addresses, postal codes, SIN and card numbers. What stays is the evidence itself:
dates, amounts, the city and province (they decide which law applies), and what happened.

Details come from two places:
- known values: the tenant's name and the rental unit from the case, the names, addresses,
  phones and emails of the Form J draft, and the caseworker's own entries;
- patterns, for details nobody entered (a phone number in a letter, the landlord's address).

Every distinct spelling gets its own placeholder, so swapping back is exact. A quote the model
copies with placeholders in it turns back into the evidence's own words, and the quote check
compares real text with real text. Spellings of one person share a stem: `[TENANT_NAME]`,
`[TENANT_NAME_2]`. Detection is never complete; the caseworker checks the list.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

# A known value starts and ends at a letter or digit only where the text has no ASCII letter or
# digit next to it. ASCII only on purpose: "Mei Chen您好" still matches "Mei Chen".
_BEFORE = r"(?<![A-Za-z0-9])"
_AFTER = r"(?![A-Za-z0-9])"
# Number patterns also refuse `_`, so they never match inside an id or a nonce.
_NUM_BEFORE = r"(?<![A-Za-z0-9_])"
_NUM_AFTER = r"(?![A-Za-z0-9_])"

TOKEN = re.compile(r"\[[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*\]")

_COMPANY_WORDS = frozenset({
    "ltd", "limited", "inc", "incorporated", "corp", "corporation", "co", "company", "llc",
    "llp", "management", "properties", "property", "holdings", "realty", "rentals", "group",
    "housing", "apartments", "developments", "enterprises", "investments", "homes", "trust",
})  # fmt: skip
# Name parts that are also ordinary words are matched only as a full name.
_COMMON_WORDS = frozenset({
    "may", "will", "mark", "bill", "rose", "june", "april", "august", "grace", "hope", "joy",
    "faith", "art", "sky", "summer", "dawn", "guy", "sue", "pat", "don", "ray", "rich", "young",
    "king", "long", "brown", "white", "black", "green", "little", "love", "hall", "street",
    "lane", "road", "unit", "the", "and", "van", "von", "de", "la", "le", "di", "da", "del",
})  # fmt: skip
_TITLES = frozenset({"mr", "mrs", "ms", "miss", "dr", "mx", "prof"})

_STREET_SUFFIX = (
    r"(?:Street|St|Road|Rd|Avenue|Ave|Drive|Dr|Lane|Ln|Court|Ct|Crescent|Cres|Boulevard|Blvd"
    r"|Way|Place|Pl|Terrace|Highway|Hwy|Row|Close|Square|Sq|Parkway|Pkwy|Circle|Cir)"
)
_UNIT_PREFIX = r"(?:(?:Unit|Apt|Apartment|Suite|Ste)\.?\s*#?\s*[A-Za-z0-9-]{1,6},?\s+)?"
_POSTAL = r"[ABCEGHJ-NPRSTVXY]\d[ABCEGHJ-NPRSTV-Z][ -]?\d[ABCEGHJ-NPRSTV-Z]\d"
_UNIT_PART = re.compile(r"(?i)^(?:unit|apt|apartment|suite|ste|#)\b")
# A day and a month are a date, not a house number and a street: "15 May Dr. Patel" and
# "3 December Court Room 2". "123 March Road" is still an address.
_MONTH = (
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\.?(?![A-Za-z])"
)
_DAY_MONTH = r"(?!(?:0?[1-9]|[12]\d|3[01])\s+" + _MONTH + r")"
# Dates written with hyphens, year first or last: a range of two looks like a card number.
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4}")
_AMOUNT = re.compile(r"^\+\d+\.\d{2}(?![\d.])")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _id_number(surface: str) -> bool:
    return _luhn(re.sub(r"\D", "", surface))


def _card_number(surface: str) -> bool:
    digits = re.sub(r"\D", "", surface)
    # Two dates side by side ("2026-08-14 2026-08-18") are a date range, not a card.
    return 13 <= len(digits) <= 19 and not _DATE.search(surface) and _luhn(digits)


def _international_phone(surface: str) -> bool:
    """Not an amount ("+850.00 2026-09-01") and not a run that holds a date."""
    if _AMOUNT.search(surface) or _DATE.search(surface):
        return False
    return 8 <= len(re.sub(r"\D", "", surface)) <= 15


_EMAIL_CHAR = "[A-Za-z0-9._%+-]"
_EMAIL = _EMAIL_CHAR + r"+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}" + _AFTER
_EMAIL_ANYWHERE = re.compile(_EMAIL)
_EMAIL_AT_RUN = re.compile(f"(?<!{_EMAIL_CHAR})" + _EMAIL)


def _emails(text: str) -> Iterable[re.Match[str]]:
    """The matches of `_EMAIL_ANYWHERE.finditer(text)`, in time linear in the length of the
    text (issue #165). Tried at every position, the local part runs to the end of a long run of
    letters and digits (OCR noise, base64) and back, once per position. A match starts either
    where such a run starts, or straight after the previous match (`jo@ex.com-ann@ex.com` gives
    `-ann@ex.com`): those are the only two places tried."""
    m = _EMAIL_AT_RUN.search(text)
    while m is not None:
        yield m
        m = _EMAIL_ANYWHERE.match(text, m.end()) or _EMAIL_AT_RUN.search(text, m.end())


@dataclass(frozen=True)
class _Pattern:
    stem: str
    kind: str
    regex: re.Pattern[str]
    check: Callable[[str], bool] | None = None
    finder: Callable[[str], Iterable[re.Match[str]]] | None = None  # else `regex.finditer`

    def finditer(self, text: str) -> Iterable[re.Match[str]]:
        return (self.finder or self.regex.finditer)(text)


PATTERNS: tuple[_Pattern, ...] = (
    _Pattern("EMAIL", "email", _EMAIL_ANYWHERE, finder=_emails),
    _Pattern("PHONE", "phone", re.compile(
        _NUM_BEFORE + r"(?:(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-])\d{3}[\s.-]\d{4}"
        r"|\+?1?[2-9]\d{2}[2-9]\d{6})" + _NUM_AFTER)),
    # Outside North America: always written with the country code (+86 138 0013 8000).
    _Pattern("PHONE", "phone", re.compile(
        r"(?<![A-Za-z0-9_+])\+(?!1[ .-]?\d{3})\d{1,3}(?:[ .-]?\d{2,4}){2,5}" + _NUM_AFTER),
        _international_phone),
    _Pattern("CARD_NUMBER", "id_number", re.compile(
        _NUM_BEFORE + r"\d(?:[ -]?\d){12,18}" + _NUM_AFTER), _card_number),
    _Pattern("ID_NUMBER", "id_number", re.compile(
        _NUM_BEFORE + r"\d{3}[ -]\d{3}[ -]\d{3}" + _NUM_AFTER), _id_number),
    _Pattern("POSTAL_CODE", "postal_code", re.compile(
        _BEFORE + _POSTAL + _AFTER, re.IGNORECASE)),
    _Pattern("ADDRESS", "address", re.compile(
        _BEFORE + _UNIT_PREFIX + _DAY_MONTH + r"\d{1,6}[A-Za-z]?"
        + r"(?:\s+[A-Z][A-Za-z'’.-]*){1,4}?\s+" + _STREET_SUFFIX + r"\b\.?")),
)  # fmt: skip


@dataclass(frozen=True)
class Known:
    """A value to hide wherever it occurs. `stem` names the placeholder (`TENANT_NAME` gives
    `[TENANT_NAME]`, a second spelling `[TENANT_NAME_2]`); a `numbered` stem counts from 1
    (`[PERSON_1]`). `exact_case` matches the value only as written (a first or last name on its
    own); otherwise case and spacing may differ. `part`: a piece of a longer value, listed for
    the caseworker only where it occurs."""

    value: str
    stem: str
    kind: str
    source: str  # "case" | "caseworker"
    exact_case: bool = False
    numbered: bool = False
    part: bool = False
    term_id: str | None = None


def normalize(value: str) -> str:
    """How two spellings of one value are compared: casefolded, spaces collapsed."""
    return " ".join(value.split()).casefold()


def looks_like_company(name: str) -> bool:
    words = {w.strip(".,()&").casefold() for w in name.split()}
    return bool(words & _COMPANY_WORDS)


def name_variants(full: str, prefix: str, source: str = "case") -> list[Known]:
    """A person's full name, and their first and last name on their own (`Hi Jordan`, `Ms. Lee`).
    A company name is not personal data and is kept as written."""
    full = " ".join(full.split())
    if len(full) < 2 or looks_like_company(full):
        return []
    out = [Known(full, f"{prefix}_NAME", "name", source)]
    words = [w.strip(".,") for w in full.split() if w.strip(".,").casefold() not in _TITLES]
    if len(words) >= 2 and all(w.isascii() and w.isalpha() for w in words):
        for word, stem in ((words[0], "FIRST_NAME"), (words[-1], "LAST_NAME")):
            if len(word) >= 2 and word.casefold() not in _COMMON_WORDS:
                out.append(Known(word, f"{prefix}_{stem}", "name", source, exact_case=True,
                                 part=True))  # fmt: skip
    return out


def address_variants(value: str, prefix: str, source: str = "case") -> list[Known]:
    """The street part of an address (`Unit 9, 77 Fictional Lane`) and its pieces. The city and
    province stay; a postal code is found by its pattern."""
    parts = [p.strip() for p in value.split(",") if p.strip()]
    place = [i for i, p in enumerate(parts)
             if any(c.isdigit() for c in p) and not re.search(_POSTAL, p, re.IGNORECASE)]
    if not place:
        return []
    lead = parts[: place[-1] + 1]
    out = [Known(", ".join(lead), f"{prefix}_ADDRESS", "address", source)]
    if len(lead) > 1:
        for p in (parts[i] for i in place):
            stem = "UNIT" if _UNIT_PART.match(p) else "STREET" if p[:1].isdigit() else ""
            if stem:
                out.append(Known(p, f"{prefix}_{stem}", "address", source, part=True))
    return out


def _known_regex(known: Known) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(w) for w in known.value.split()).replace(r",\s+", r",\s*")
    before = _BEFORE if known.value[:1].isascii() and known.value[:1].isalnum() else ""
    after = _AFTER if known.value[-1:].isascii() and known.value[-1:].isalnum() else ""
    return re.compile(before + body + after, 0 if known.exact_case else re.IGNORECASE)


@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    stem: str
    kind: str
    source: str
    numbered: bool
    part: bool
    term_id: str | None
    rank: int  # 0 for a known value, 1 for a pattern: a known value wins a tie


@dataclass
class Entry:
    """One placeholder and the exact text it stands for."""

    value: str
    placeholder: str
    kind: str
    source: str  # "case" | "found" | "caseworker"
    part: bool = False
    term_id: str | None = None
    count: int = 0


@dataclass
class Placeholders:
    """Swaps personal details for placeholders (`hide`) and back (`reveal`). Placeholders are
    handed out in the order values are first seen, so building one the same way from the same
    case gives the same placeholders. `shown` values (normalized) are left as written."""

    known: Iterable[Known] = ()
    shown: Iterable[str] = ()
    _known: list[tuple[Known, re.Pattern[str]]] = field(default_factory=list, init=False)
    _shown: set[str] = field(default_factory=set, init=False)
    _by_value: dict[str, str] = field(default_factory=dict, init=False)
    _entries: dict[str, Entry] = field(default_factory=dict, init=False)
    _next: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self._shown = {normalize(v) for v in self.shown}
        first: dict[str, int] = {}
        for k in self.known:
            key = normalize(k.value)
            if len(key) < 2:
                continue
            i = first.get(key)
            if i is None:
                first[key] = len(self._known)
                self._known.append((k, _known_regex(k)))
            elif k.term_id is not None and self._known[i][0].term_id is None:
                # The caseworker hid a value the case already hides (issue #166): their term
                # goes on the case's value, so the list shows it and Remove undoes it. How the
                # value is matched, and its placeholder, stay the case's.
                kept, regex = self._known[i]
                self._known[i] = (replace(kept, source=k.source, term_id=k.term_id), regex)
        for k, _ in self._known:  # the case's own spelling gets the plain placeholder
            if normalize(k.value) not in self._shown and not k.part:
                self._token(k.value, k.stem, k.kind, k.source, k.numbered, k.part, k.term_id)

    # ------------------------------------------------------------------ placeholders

    def _token(
        self, surface: str, stem: str, kind: str, source: str, numbered: bool, part: bool,
        term_id: str | None,
    ) -> str:
        token = self._by_value.get(surface)
        if token is not None:
            return token
        n = self._next.get(stem, 1)
        self._next[stem] = n + 1
        token = f"[{stem}_{n}]" if numbered or n > 1 else f"[{stem}]"
        self._by_value[surface] = token
        self._entries[token] = Entry(surface, token, kind, source, part, term_id)
        return token

    def _spans(self, text: str) -> list[_Span]:
        found: list[_Span] = []
        for k, regex in self._known:
            for m in regex.finditer(text):
                found.append(_Span(m.start(), m.end(), k.stem, k.kind, k.source, k.numbered,
                                   k.part, k.term_id, 0))  # fmt: skip
        for p in PATTERNS:
            for m in p.finditer(text):
                if p.check is None or p.check(m.group(0)):
                    found.append(_Span(m.start(), m.end(), p.stem, p.kind, "found", True,
                                       False, None, 1))  # fmt: skip
        found.sort(key=lambda s: (s.start, s.start - s.end, s.rank))
        chosen: list[_Span] = []
        end = 0
        for s in found:
            if s.start >= end:  # the longest match at the leftmost place wins
                chosen.append(s)
                end = s.end
        return chosen

    def hide(self, text: str, *, count: bool = False) -> str:
        """The text with each personal detail replaced by its placeholder. `count` adds the
        occurrences to the list the caseworker sees (used once per evidence page)."""
        out: list[str] = []
        pos = 0
        for s in self._spans(text):
            surface = text[s.start : s.end]
            out.append(text[pos : s.start])
            pos = s.end
            if normalize(surface) in self._shown:
                out.append(surface)
                continue
            token = self._token(surface, s.stem, s.kind, s.source, s.numbered, s.part, s.term_id)
            out.append(token)
            if count:
                self._entries[token].count += 1
        out.append(text[pos:])
        return "".join(out)

    def reveal(self, text: str) -> str:
        """The model's text with every placeholder it was given turned back into the real value.
        A placeholder it made up stays as written."""
        return TOKEN.sub(lambda m: self._entry_value(m.group(0)), text)

    def _entry_value(self, token: str) -> str:
        entry = self._entries.get(token)
        return entry.value if entry is not None else token

    def snapshot(self) -> dict[str, Any]:
        """Every placeholder handed out so far and the counters, as JSON, to save with a run."""
        return {
            "entries": [[e.value, e.placeholder, e.kind, e.source, e.part, e.term_id, e.count]
                        for e in self._entries.values()],
            "next": dict(self._next),
        }  # fmt: skip

    def restore(self, saved: dict[str, Any]) -> None:
        """Go back to a saved `snapshot`: each value keeps its placeholder, and a value first
        seen from now on gets the next free number. A run that resumes after a pause must not
        renumber: its saved conversation still holds the old placeholders."""
        self._entries.clear()
        self._by_value.clear()
        for value, token, kind, source, part, term_id, count in saved["entries"]:
            self._entries[token] = Entry(value, token, kind, source, part, term_id, count)
            self._by_value[value] = token
        self._next = {str(k): int(v) for k, v in saved["next"].items()}

    def entries(self) -> list[Entry]:
        """Every placeholder handed out so far, in order. Pieces of a longer value (a first name
        on its own) are listed only where they occurred, or when the caseworker hid them."""
        listed = [e for e in self._entries.values() if not e.part or e.count or e.term_id]
        terms = {e.term_id for e in listed}
        for k, _ in self._known:  # a piece the caseworker hid that never occurred on its own
            if k.term_id is not None and k.term_id not in terms:
                listed.append(Entry(k.value, self._unseen_placeholder(k), k.kind, k.source,
                                    k.part, k.term_id))  # fmt: skip
        return listed

    def _unseen_placeholder(self, known: Known) -> str:
        """The placeholder of a value no text has shown yet, when nothing read later can change
        it: one word matched only as written, the only value of its stem, no number handed out.
        Else "": which number it gets depends on what is read first."""
        alone = sum(k.stem == known.stem for k, _ in self._known) == 1
        sure = known.exact_case and not known.numbered and len(known.value.split()) == 1
        return f"[{known.stem}]" if sure and alone and known.stem not in self._next else ""


# A caseworker's own entry: what it is decides its placeholder. A person or an address the
# finder missed gets a numbered one; another spelling of the tenant's name shares their stem.
_ROLE_STEMS: dict[str, tuple[str, str, bool]] = {
    "tenant": ("TENANT_NAME", "name", False),
    "landlord": ("LANDLORD_NAME", "name", False),
    "person": ("PERSON", "name", True),
    "address": ("ADDRESS", "address", True),
    "other": ("DETAIL", "other", True),
}


def caseworker_known(value: str, role: str, term_id: str | None = None) -> Known:
    stem, kind, numbered = _ROLE_STEMS.get(role, _ROLE_STEMS["other"])
    return Known(value, stem, kind, "caseworker", numbered=numbered, term_id=term_id)
