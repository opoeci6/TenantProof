"""Deterministic form narrative (CONTRACT_CHANGES #1). Pure: no I/O, no LLM.

Only events with review_status accepted/edited and at least one source are used, so
rejected, pending and unsourced events never reach the form. caseworker_note is never read here.
"""

from collections.abc import Mapping, Sequence
from typing import Any

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
INCLUDED = frozenset({"accepted", "edited"})


def format_date(date: Mapping[str, Any] | None, long: bool = False) -> str:
    if not date or not date.get("value") or date.get("precision") == "unknown":
        return "Date unknown"
    parts = str(date["value"]).split("-")
    year, month = int(parts[0]), int(parts[1])
    if date.get("precision") == "month":
        return f"{MONTHS[month - 1]} {year}" + (" (month only)" if long else "")
    return f"{MONTHS[month - 1]} {int(parts[2])}, {year}"


def build_narrative(
    events: Sequence[Mapping[str, Any]], labels: Mapping[str, str]
) -> dict[str, Any]:
    """events must already be sorted by date. labels maps evidence_id -> exhibit label."""
    # No source, no fact (API_CONTRACT design rule 2): the PATCH refuses to accept such events,
    # and this filter keeps any that arrive another way out of the form.
    used = [e for e in events if e["review_status"] in INCLUDED and e["sources"]]
    lines: list[str] = []
    for e in used:
        seen: list[str] = []
        for s in e["sources"]:
            label = labels.get(s["evidence_id"], s["evidence_id"])
            if label not in seen:
                seen.append(label)
        cite = f" [{', '.join(seen)}]"
        lines.append(f"{format_date(e['date'], True)}: {e['description']}{cite}")
    return {"text": "\n".join(lines), "event_ids": [e["id"] for e in used]}
