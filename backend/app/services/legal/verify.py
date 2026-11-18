"""The source check: does the official document still say what we cite it for? (WS4)

`check(entry, fetch)` is pure apart from the `fetch` it is given, so tests pass fixture bytes and
never touch the network. The command at the bottom is the only thing here that uses httpx, and
**nothing calls it from inside a run**: a run reads the stored `verify_status`, it never waits on
the internet.

What each status means:

- `unverified`: nobody has confirmed the entry (`confirmed_by` is unset) or it has no excerpt, so
  there is nothing to compare. This is not a failure.
- `unreachable`: the fetch failed or timed out. `last_verified_at` is left alone, because no check
  happened.
- `unchanged`: the excerpt is still in the document (fuzzy match of at least `THRESHOLD`).
- `changed`: the document was fetched and the excerpt was not found in it. An image-only PDF that
  yields no text lands here too: what we rely on is not in the text the URL serves.

The comparison is a fuzzy one, not equality, because the same sentence arrives with different
line breaks, hyphenation and spacing from year to year. The threshold is the citation verifier's
(`services/verifier.py`), so "the quote is really there" means the same thing everywhere.

One thing the fuzzy score cannot see: a changed **number**. Rewriting "5 days" as "10 days"
inside a 200-character sentence still scores 99, which would report a changed deadline as
`unchanged` — the one change this project exists to catch. So the passage that best matches the
excerpt must also still contain every number the excerpt relies on.
"""

import html
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx
from rapidfuzz import fuzz
from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from app.models import LegalSourceRow
from app.services import activity
from app.services.evidence import text as evidence_text
from app.services.legal.corpus import CORPUS, CorpusEntry

VerifyStatus = Literal["unchanged", "changed", "unreachable", "unverified"]
THRESHOLD = 90  # same as services/verifier.py
# A statute PDF is 1.7 MB and a slow link needs more than ten seconds to read it (25 s here on
# one that works), while a host that never answers should not hold the command forever:
# connecting is quick to fail, reading a real document is allowed to take longer.
CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0
ACTOR_NAME = "TenantProof source check"
_USER_AGENT = "TenantProof source check (tenant legal clinic demo)"


@dataclass(frozen=True)
class Fetched:
    """What a fetch returns: the body and its declared type. Bytes, so a PDF stays a PDF."""

    content: bytes
    content_type: str


Fetch = Callable[[str], Fetched]

_TAGS = re.compile(r"<[^>]+>")
_SCRIPT_OR_STYLE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
_SPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"\d+")


def normalise(value: str) -> str:
    """One line of single spaces. The comparison is about words, not layout."""
    return _SPACE.sub(" ", value).strip()


def extract_text(fetched: Fetched) -> str:
    """The document's text: PDFs through the evidence reader (trusted official PDFs, so the
    parent process is fine), everything else as HTML with the tags stripped."""
    if fetched.content.startswith(b"%PDF-") or "pdf" in fetched.content_type.casefold():
        pdf = evidence_text.read_pdf(fetched.content)
        return normalise("\n".join(page["text"] for page in pdf.pages))
    body = fetched.content.decode("utf-8", errors="replace")
    return normalise(html.unescape(_TAGS.sub(" ", _SCRIPT_OR_STYLE.sub(" ", body))))


def check(entry: CorpusEntry, fetch: Fetch) -> VerifyStatus:
    """Compare the entry's excerpt with the document at its URL. Never raises: a fetch that
    fails, times out or serves something unreadable is a status, not an exception."""
    if entry.confirmed_by is None or not entry.excerpt.strip():
        return "unverified"
    if entry.url is None:
        return "unverified"
    try:
        fetched = fetch(entry.url)
        found = extract_text(fetched)
    except Exception:  # any transport, decoding or PDF error means we could not check
        return "unreachable"
    if not found:
        return "changed"
    excerpt = normalise(entry.excerpt)
    alignment = fuzz.partial_ratio_alignment(excerpt, found)
    if alignment is None or alignment.score < THRESHOLD:
        return "changed"
    # The passage that best matches, so a page number elsewhere in the document cannot satisfy
    # the excerpt's own numbers (see the module docstring).
    passage = found[alignment.dest_start:alignment.dest_end]
    if any(n not in _NUMBER.findall(passage) for n in _NUMBER.findall(excerpt)):
        return "changed"
    return "unchanged"


def fetch_url(url: str) -> Fetched:
    """The real fetch, with a hard timeout. Fragments (`#page=4`) are not sent, so two entries
    of one document download it once each and compare different excerpts."""
    timeout = httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
    response = httpx.get(
        url, timeout=timeout, follow_redirects=True, headers={"user-agent": _USER_AGENT}
    )
    response.raise_for_status()
    content_type = response.headers.get("content-type", "")
    return Fetched(content=response.content, content_type=content_type)


def update_rows(
    session: Session, entry: CorpusEntry, status: VerifyStatus, now: datetime
) -> list[str]:
    """Set the status on every row of every case that carries this entry's URL, and return the
    case ids touched. `last_verified_at` moves only when text was actually compared. An
    unreachable document keeps the date of its last real check; an unconfirmed entry clears it,
    since a row that is not verified must not show when it was "last checked".

    A row is the entry's by URL or, like `rules.row_for`, by title: a row made before the corpus
    moved a document to a new address still carries the old one, and is moved to the new one
    here, so it is checked instead of staying `unverified` for good."""
    if entry.url is None:
        return []
    rows = session.exec(
        select(LegalSourceRow).where(
            or_(
                col(LegalSourceRow.url) == entry.url,
                func.lower(LegalSourceRow.title) == entry.title.lower(),
            )
        )
    ).all()
    for row in rows:
        row.url = entry.url
        row.verify_status = status
        if status in ("unchanged", "changed"):
            row.last_verified_at = now
        elif status == "unverified":
            row.last_verified_at = None
        session.add(row)
    return sorted({row.case_id for row in rows})


def _summary(statuses: list[VerifyStatus]) -> str:
    counts = {s: statuses.count(s) for s in sorted(set(statuses))}
    return ", ".join(f"{n} {s}" for s, n in counts.items())


def run(
    fetch: Fetch = fetch_url, session: Session | None = None
) -> list[tuple[CorpusEntry, VerifyStatus]]:
    """Check every entry and write the results. Returns what each entry came back as. Pass a
    session to reuse one (tests); otherwise it opens its own and commits."""
    if session is None:
        from sqlmodel import Session as _Session

        from app.db import get_engine

        with _Session(get_engine()) as own:
            results = _run(fetch, own)
            own.commit()
        return results
    return _run(fetch, session)


def _run(fetch: Fetch, session: Session) -> list[tuple[CorpusEntry, VerifyStatus]]:
    now = datetime.now(UTC)
    results: list[tuple[CorpusEntry, VerifyStatus]] = []
    per_case: dict[str, list[VerifyStatus]] = {}
    for entry in CORPUS:
        status = check(entry, fetch)
        results.append((entry, status))
        for case_id in update_rows(session, entry, status, now):
            per_case.setdefault(case_id, []).append(status)
    for case_id, statuses in per_case.items():
        activity.log(
            session, case_id, "agent", ACTOR_NAME,
            f"Checked the official sources: {_summary(statuses)}",
        )  # fmt: skip
    return results


def main() -> None:
    """`uv run python -m app.services.legal.verify`: check every entry against its live URL and
    update every case's rows. Run it by hand before a demo; it is never part of a run."""
    print(f"Checking {len(CORPUS)} sources (a slow link can take a minute)...")
    for entry, status in run():
        print(f"{status:12} {entry.id}")
        if status == "changed":
            print(f"             the excerpt is no longer in {entry.url}")


if __name__ == "__main__":
    main()
