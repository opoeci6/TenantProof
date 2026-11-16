"""The case brief PDF: every statement names the upload it came from or says NEEDS CASEWORKER
VERIFICATION, rejected events and caseworker notes are left out, and the endpoint refuses a case
that has not been analysed."""

import io
import re
from datetime import UTC, datetime

import pytest
from pypdf import PdfReader
from sqlmodel import Session, delete, select

from app.models import (
    ActivityRow,
    EvidencePageRow,
    EvidenceRow,
    GapRow,
    RunRow,
    TimelineEventRow,
)
from app.services.case_brief import NEEDS

CASE = "case_demo_001"
LIVE = "case_live_001"
URL = "/api/cases/{}/case-brief"


def pdf_text(response) -> str:
    assert response.status_code == 200, response.text
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(response.content)).pages)


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


class Text(str):
    """The PDF's text with all whitespace removed. The PDF wraps lines at any character, so a
    phrase can be split across lines anywhere; needles are squashed the same way."""

    def __new__(cls, raw: str) -> "Text":
        return super().__new__(cls, squash(raw))

    def __contains__(self, item: object) -> bool:
        return super().__contains__(squash(str(item)))

    def index(self, item: str, *args: int) -> int:  # type: ignore[override]
        return super().index(squash(item), *args)

    def count(self, item: str, *args: int) -> int:  # type: ignore[override]
        return super().count(squash(item), *args)


def brief(client, case: str = CASE) -> str:
    return Text(pdf_text(client.get(URL.format(case))))


def add_event(session: Session, **kw) -> TimelineEventRow:
    row = TimelineEventRow(
        **{
            "id": "te_brief1",
            "case_id": CASE,
            "date": {"value": "2026-09-30", "precision": "day", "basis": "message_timestamp"},
            "sort_key": "2026-09-30",
            "title": "Extra event",
            "description": "Something happened.",
            "actor": "tenant",
            "sources": [],
            "strength": "single_source",
            "strength_reasons": [],
            "flags": [],
            "review_status": "pending",
            **kw,
        }
    )
    session.add(row)
    session.commit()
    return row


# --------------------------------------------------------------------------- the endpoint


def test_the_demo_case_produces_a_pdf(fresh_db: None, client) -> None:
    r = client.get(URL.format(CASE))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert (
        r.headers["content-disposition"] == f'inline; filename="tenantproof-case-brief-{CASE}.pdf"'
    )
    assert r.headers["cache-control"] == "no-store"
    assert r.content.startswith(b"%PDF")


def test_an_unknown_case_and_another_orgs_case_are_404(
    fresh_db: None, client, other_org_client
) -> None:
    assert client.get(URL.format("case_nope")).status_code == 404
    assert other_org_client.get(URL.format(CASE)).status_code == 404


def test_a_signed_out_request_is_refused(fresh_db: None, anon_client) -> None:
    assert anon_client.get(URL.format(CASE)).status_code == 401


def test_a_case_with_no_completed_run_is_409_with_a_plain_reason(fresh_db: None, client) -> None:
    r = client.get(URL.format(LIVE))
    assert r.status_code == 409
    assert r.json()["detail"]["reasons"] == [
        "No analysis has been completed for this case yet. Run the analysis first."
    ]


@pytest.mark.parametrize(
    ("status", "finished"), [("running", False), ("needs_review", False), ("failed", True)]
)
def test_a_paused_running_or_failed_run_does_not_count(
    fresh_db: None, client, session: Session, status: str, finished: bool
) -> None:
    session.add(
        RunRow(
            id="run_b",
            case_id=LIVE,
            status=status,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC) if finished else None,
            steps=[],
        )
    )
    session.commit()
    assert client.get(URL.format(LIVE)).status_code == 409


def test_building_the_brief_changes_nothing(fresh_db: None, client, session: Session) -> None:
    def counts() -> tuple:
        session.expire_all()
        return tuple(
            len(session.exec(select(m)).all())
            for m in (ActivityRow, TimelineEventRow, GapRow, EvidenceRow)
        )

    before = counts()
    brief(client)
    assert counts() == before


# --------------------------------------------------------------------------- what it says


def test_it_has_the_four_sections_and_the_case(fresh_db: None, client) -> None:
    text = brief(client)
    for heading in (
        "1. Case summary",
        "2. Timeline",
        "3. Evidence index",
        "4. Gaps and items for caseworker review",
        "5. Verification checklist",
    ):
        assert heading in text
    assert "Bathroom ceiling leak and mould" in text and "Jordan Lee" in text


def test_the_timeline_lists_events_in_date_order_and_leaves_out_rejected(
    fresh_db: None, client, session: Session
) -> None:
    add_event(
        session,
        id="te_rej",
        title="Rejected by the caseworker",
        review_status="rejected",
        sort_key="2026-01-01",
    )
    text = brief(client)
    assert "Rejected by the caseworker" not in text
    rows = session.exec(
        select(TimelineEventRow).where(
            TimelineEventRow.case_id == CASE, TimelineEventRow.review_status != "rejected"
        )
    ).all()
    ordered = [e.title for e in sorted(rows, key=lambda e: (e.sort_key or "9", e.title))]
    positions = [text.index(title) for title in ordered]
    assert positions == sorted(positions)


def test_review_state_is_shown_and_pending_says_so(fresh_db: None, client) -> None:
    text = brief(client)
    assert "Accepted." in text and "Not yet reviewed by a caseworker" in text


def test_every_event_names_the_file_it_came_from(fresh_db: None, client, session: Session) -> None:
    text = brief(client)
    for event in session.exec(select(TimelineEventRow).where(TimelineEventRow.case_id == CASE)):
        for src in event.sources:
            label = session.get(EvidenceRow, src["evidence_id"]).label  # type: ignore[union-attr]
            assert f"{label} (" in text


def test_an_event_without_a_source_needs_verification(
    fresh_db: None, client, session: Session
) -> None:
    add_event(session, title="Unsupported claim")
    text = brief(client)
    assert f"{NEEDS}: timeline event “Unsupported claim” has no supporting upload" in text
    assert text.count("[ ] timeline event “Unsupported claim”") == 1


def test_a_source_that_is_another_cases_file_does_not_support_an_event(
    fresh_db: None, client, session: Session
) -> None:
    add_event(
        session,
        title="Borrowed claim",
        sources=[
            {"evidence_id": "ev_of_another_case", "locator": "page 1", "excerpt": "an excerpt here"}
        ],
    )
    text = brief(client)
    assert "a source that is not an uploaded file of this case" in text
    assert "timeline event “Borrowed claim” has no supporting upload" in text


def stored_page(session: Session, text: str, evidence: str = "ev_04", locator: str = "message 1"):
    session.exec(delete(EvidencePageRow).where(EvidencePageRow.evidence_id == evidence))  # type: ignore[call-overload]
    session.add(
        EvidencePageRow(evidence_id=evidence, locator=locator, case_id=CASE, position=0, text=text)
    )
    session.commit()


def test_a_source_is_checked_again_against_the_stored_text(
    fresh_db: None, client, session: Session
) -> None:
    stored_page(session, "Water has been dripping from the bathroom ceiling since last night.")
    add_event(
        session,
        title="Checked event",
        sources=[
            {
                "evidence_id": "ev_04",
                "locator": "message 1",
                "excerpt": "dripping from the bathroom ceiling since last night",
            }
        ],
    )
    add_event(
        session,
        id="te_brief2",
        title="Invented event",
        sources=[
            {
                "evidence_id": "ev_04",
                "locator": "message 1",
                "excerpt": "the landlord admitted the roof was never repaired",
            }
        ],
    )
    text = brief(client)
    assert "[checked against the stored text]" in text
    assert "[NOT found in the stored text]" in text
    assert "timeline event “Invented event” has no supporting upload" in text
    assert "timeline event “Checked event” has no supporting upload" not in text


def test_the_tenant_and_unit_are_supported_only_when_an_upload_has_them(
    fresh_db: None, client, session: Session
) -> None:
    first = brief(client)  # the seeded case has no stored text at all
    assert "Tenant is not in any uploaded file" in first
    stored_page(session, "Hello, this is Jordan Lee at Unit 4B, 100 Example Street, Halifax, NS.")
    second = brief(client)
    assert "Tenant: Jordan Lee  [found in E-04 message 1]" in second
    assert (
        "Rental unit: Unit 4B, 100 Example Street, Halifax, NS  [found in E-04 message 1]" in second
    )


def test_a_case_record_fact_is_labelled_and_a_missing_hearing_date_is_flagged(
    fresh_db: None, client
) -> None:
    text = brief(client)
    assert "[case record, entered by the clinic]" in text
    assert f"Hearing date: {NEEDS}: no hearing date has been entered" in text


def test_an_entered_hearing_date_is_labelled_as_entered_by_a_caseworker(
    fresh_db: None, hearing_known: None, client
) -> None:
    assert "Hearing date: 2026-11-24  [entered by a caseworker, not from an upload]" in brief(
        client
    )


def test_the_count_at_the_top_matches_the_checklist(fresh_db: None, client) -> None:
    text = brief(client)
    count = int(re.search(r"(\d+)items?needcaseworkerverification", text).group(1))  # type: ignore[union-attr]
    assert count > 0 and text.count("[ ] ") == count


def test_a_caseworker_note_never_appears(fresh_db: None, client, session: Session) -> None:
    session.exec(select(TimelineEventRow))  # noqa: B018
    event = session.exec(select(TimelineEventRow).where(TimelineEventRow.case_id == CASE)).first()
    assert event is not None
    event.caseworker_note = "SECRET-INTERNAL-NOTE do not share"
    session.add(event)
    session.commit()
    assert "SECRET-INTERNAL-NOTE" not in brief(client)


# --------------------------------------------------------------------------- evidence and gaps


def index_section(text: str) -> str:
    return text[text.index("3. Evidence index") : text.index("4. Gaps and items")]


def test_the_evidence_index_is_sorted_by_type_then_date(fresh_db: None, client) -> None:
    section = Text(index_section(brief(client)))
    names = ["lease_2026.pdf", "messages_landlord.txt", "dehumidifier_receipt.pdf", "IMG_4412.HEIC"]
    positions = [section.index(name) for name in names]  # lease, messages, receipt, photo
    assert positions == sorted(positions)


def test_photos_in_the_index_are_ordered_by_capture_time(fresh_db: None, client) -> None:
    section = Text(index_section(brief(client)))
    assert section.index("IMG_4412.HEIC") < section.index("IMG_4419.HEIC")  # 21:03 before 21:05


def test_each_file_shows_its_fingerprint_and_how_often_it_is_cited(
    fresh_db: None, client, session: Session
) -> None:
    section = Text(index_section(brief(client)))
    cited: dict[str, int] = {}
    for event in session.exec(select(TimelineEventRow).where(TimelineEventRow.case_id == CASE)):
        for src in event.sources:
            cited[src["evidence_id"]] = cited.get(src["evidence_id"], 0) + 1
    for ev in session.exec(select(EvidenceRow).where(EvidenceRow.case_id == CASE)):
        assert ev.sha256 in section and f"{ev.label} {ev.filename}" in section
        n = cited.get(ev.id, 0)
        assert f"SHA-256 {ev.sha256}. Cited by {n} timeline event{'s' if n != 1 else ''}" in section


def test_a_pdf_the_server_calls_other_is_a_document(
    fresh_db: None, client, session: Session
) -> None:
    ev = session.exec(select(EvidenceRow).where(EvidenceRow.case_id == CASE)).first()
    assert ev is not None
    ev.kind, ev.mime = "other", "application/pdf"
    session.add(ev)
    session.commit()
    assert "(document, uploaded by" in brief(client)


def test_an_unprocessed_file_is_flagged(fresh_db: None, client, session: Session) -> None:
    ev = session.exec(select(EvidenceRow).where(EvidenceRow.case_id == CASE)).first()
    assert ev is not None
    ev.processing = "failed"
    session.add(ev)
    session.commit()
    text = brief(client)
    assert f"{ev.label} was not processed (failed)" in text


def test_open_gaps_are_listed_blocking_first_and_resolved_ones_are_not(
    fresh_db: None, client, session: Session
) -> None:
    gaps = session.exec(select(GapRow).where(GapRow.case_id == CASE)).all()
    done = gaps[0]
    done.status = "resolved"
    session.add(done)
    session.commit()
    text = brief(client)
    assert done.title not in text
    open_ = [g for g in gaps if g.status in ("open", "waiting_tenant") and g is not done]
    ordered = sorted(open_, key=lambda g: (g.severity != "blocking", g.position))
    positions = [text.index(g.title) for g in ordered]
    assert positions == sorted(positions)


def test_a_chinese_upload_and_name_render(fresh_db: None, client, session: Session) -> None:
    stored_page(session, "[2026-09-25 20:41] 陈梅: 我自己买了除湿机，花了189.99元。")
    add_event(
        session,
        title="买除湿机",
        sources=[
            {
                "evidence_id": "ev_04",
                "locator": "message 1",
                "excerpt": "我自己买了除湿机，花了189.99元",
            }
        ],
    )
    r = client.get(URL.format(CASE))
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert "checked against the stored text" in Text(pdf_text(r))


# --------------------------------------------------------------------------- the gaps step


def set_gaps_step(session: Session, status: str | None) -> None:
    run = session.exec(select(RunRow).where(RunRow.case_id == CASE)).first()
    assert run is not None
    steps = [dict(step) for step in run.steps if step["key"] != "gaps"]
    if status is not None:
        steps.append(
            {
                "key": "gaps",
                "label": "Find missing information",
                "status": status,
                "summary": None,
                "tool_calls": [],
            }
        )
    run.steps = steps
    session.add(run)
    session.commit()


@pytest.mark.parametrize("status", ["pending", "needs_input", "failed", None])
def test_a_gaps_step_that_did_not_complete_is_flagged(
    fresh_db: None, client, session: Session, status: str | None
) -> None:
    set_gaps_step(session, status)
    text = brief(client)
    assert f"{NEEDS}: the gaps step has not completed for this case" in text
    assert "[ ] the gaps step has not completed" in text


def test_a_completed_gaps_step_is_not_flagged(fresh_db: None, client, session: Session) -> None:
    set_gaps_step(session, "done")
    assert "the gaps step has not completed" not in brief(client)


def test_an_empty_gap_list_says_none_are_recorded_not_none_exist(
    fresh_db: None, client, session: Session
) -> None:
    session.exec(delete(GapRow).where(GapRow.case_id == CASE))  # type: ignore[call-overload]
    session.commit()
    assert "No open items are recorded." in brief(client)


def test_files_added_after_the_last_analysis_are_flagged(
    fresh_db: None, client, session: Session
) -> None:
    run = session.exec(select(RunRow).where(RunRow.case_id == CASE)).first()
    assert run is not None
    run.stale = True
    session.add(run)
    session.commit()
    assert f"{NEEDS}: files were added after the last analysis" in brief(client)
