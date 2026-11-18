"""Date choice and date flags (BUILD_PLAN section 6). Pure: no I/O, no LLM.

The model proposes date candidates; this module picks one and decides the flags."""

import re
from datetime import date, timedelta
from typing import Any

from app.schemas import EventDate

# Highest priority first. `user` is only ever set by a caseworker, never by the agent.
BASIS_PRIORITY = ("message_timestamp", "exif", "document_text", "filename", "user")
_PRECISION_RANK = {"day": 0, "month": 1, "unknown": 2}


def _rank(candidate: EventDate) -> tuple[int, int]:
    return BASIS_PRIORITY.index(candidate.basis), _PRECISION_RANK[candidate.precision]


def choose_date(candidates: list[EventDate]) -> EventDate:
    """The candidate with the best basis; a day beats a month on the same basis; the earlier
    listed wins a tie. Candidates with an unknown date are chosen only when nothing else is."""
    known = [c for c in candidates if c.precision != "unknown"]
    pool = known or candidates
    return min(pool, key=_rank)  # min() keeps the first of equal keys


def _as_date(candidate: EventDate) -> date | None:
    if candidate.value is None or candidate.precision != "day":
        return None
    return date.fromisoformat(candidate.value)


def _month(candidate: EventDate) -> str | None:
    return None if candidate.value is None else candidate.value[:7]


def date_mismatch(candidates: list[EventDate]) -> bool:
    """True when two known candidates disagree: day precision on both and more than 1 day
    apart, or any month precision and different months."""
    known = [c for c in candidates if c.precision != "unknown" and c.value is not None]
    for i, a in enumerate(known):
        for b in known[i + 1 :]:
            if a.precision == "day" and b.precision == "day":
                da, db = _as_date(a), _as_date(b)
                if da is not None and db is not None and abs((da - db).days) > 1:
                    return True
            elif _month(a) != _month(b):
                return True
    return False


def date_inferred(chosen: EventDate) -> bool:
    """The date comes from a filename, not from the content or metadata."""
    return chosen.basis == "filename"


def exif_matches(candidate: EventDate, captured_times: list[str]) -> bool:
    """An `exif` date must be the capture date of one of the cited files (ISO timestamps)."""
    if candidate.value is None:
        return False
    size = 10 if candidate.precision == "day" else 7
    return any(t[:size] == candidate.value for t in captured_times)


# --------------------------------------------------------------------------- hearing date

_MONTH_NAMES = (
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",),
    ("june", "jun"), ("july", "jul"), ("august", "aug"), ("september", "sept", "sep"),
    ("october", "oct"), ("november", "nov"), ("december", "dec"),
)
_MONTHS = {name: i for i, names in enumerate(_MONTH_NAMES, start=1) for name in names}
HEARING_FIRST, HEARING_LAST = date(2020, 1, 1), date(2099, 12, 31)
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_FIRST = re.compile(r"^([a-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})$")
_DAY_FIRST = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)? ([a-z]{3,9})\.?,? (\d{4})$")


def parse_hearing_date(text: str) -> date | None:
    """A caseworker's typed hearing date: `2026-11-21`, `Nov 21, 2026` or `21 November 2026`
    (English month names, any case). A real calendar date from 2020 to 2099, else None. No
    relative dates ("next Friday") and no guessing between day and month."""
    value = " ".join(text.lower().split())
    try:
        if m := _ISO.match(value):
            parsed = date(int(m[1]), int(m[2]), int(m[3]))
        elif m := _MONTH_FIRST.match(value):
            parsed = date(int(m[3]), _MONTHS[m[1]], int(m[2]))
        elif m := _DAY_FIRST.match(value):
            parsed = date(int(m[3]), _MONTHS[m[2]], int(m[1]))
        else:
            return None
    except (KeyError, ValueError):  # not a month name, or not a calendar date
        return None
    return parsed if HEARING_FIRST <= parsed <= HEARING_LAST else None


def hearing_has_passed(hearing: date, today: date) -> bool:
    """A hearing date before today. Today itself is still upcoming."""
    return hearing < today


EVIDENCE_DAYS_BEFORE_HEARING = 5  # Evidence Policy #42; Regulations s. 25A(2)


DEADLINE_PREFIX = "Must be received by"
_LEGACY_PREFIX = re.compile(r"^\s*evidence due\b", re.IGNORECASE)
_LABEL_HEARING = re.compile(r"hearing (\d{4}-\d{2}-\d{2})")


def canonical_deadline_label(label: str) -> str:
    """A stored deadline label in today's wording. Labels written before the wording changed say
    "Evidence due"; the date is a receipt date (Regulations s. 25A(2)), so they say "Must be
    received by" now, whenever the row was stored."""
    return _LEGACY_PREFIX.sub(DEADLINE_PREFIX, label, count=1)


def demo_deadline(deadline: dict[str, Any], today: date) -> dict[str, str]:
    """The deadline of a seeded demo case, `demo_offset_days` from today, so a database seeded
    days ago never shows a past deadline. The hearing date in the label moves with it. Any other
    deadline is returned as stored, in the current wording."""
    label = canonical_deadline_label(str(deadline["label"]))
    due = str(deadline["date"])
    offset = deadline.get("demo_offset_days")
    if offset is None:
        return {"date": due, "label": label}
    moved = today + timedelta(days=int(offset))
    shift = moved - date.fromisoformat(due)
    label = _LABEL_HEARING.sub(
        lambda m: f"hearing {(date.fromisoformat(m.group(1)) + shift).isoformat()}", label
    )
    return {"date": moved.isoformat(), "label": label}


def evidence_deadline(hearing: date, days: int = EVIDENCE_DAYS_BEFORE_HEARING) -> dict[str, str]:
    """`Case.next_deadline` for a hearing: the day evidence must be *received* by, `days` before
    it (Regulations s. 25A(2)). It is a receipt date, not a send-by date: registered mail,
    express post or courier counts as received on the 3rd day after it is sent, and e-mail
    sent at or after 4 p.m. the next day (s. 25A(3B), (3C)). The label has the ISO hearing date
    in it, like the seeded cases. The rules step
    passes the number the legal corpus carries, so a change to the policy moves this deadline
    with it."""
    due = hearing - timedelta(days=days)
    label = f"{DEADLINE_PREFIX} · hearing {hearing.isoformat()}"
    return {"date": due.isoformat(), "label": label}
