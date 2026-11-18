"""Seed Postgres from seed_data.json (dumped from the prototype's mock data).

Run: uv run python -m app.seed [--force]   (seeds only an empty DB unless --force wipes and reloads)
Deadlines in the dump are relative to its `dumped_on` date, so they are shifted to today, and
kept relative to today when read (`demo_offset_days`)."""

import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete
from sqlmodel import Session, select

from app.auth import hash_password
from app.config import get_settings
from app.db import get_engine
from app.models import (
    ActivityRow,
    CaseRow,
    EvidenceRow,
    ExcerptTranslationRow,
    FormFieldRow,
    GapRow,
    IntakeTokenRow,
    LegalSourceRow,
    LoginSessionRow,
    OrgRow,
    PackageRow,
    RunRow,
    TimelineEventRow,
    UserRow,
)
from app.services.intake_token import issue_token
from app.services.legal.corpus import FORM_J_DRAFT_LABEL

DATA = Path(__file__).parent / "seed_data.json"
CASEWORKER_EMAIL = "sam@example-clinic.test"
OTHER_ORG = {"id": "org_harbourside", "name": "Harbourside Tenant Legal Clinic"}
OTHER_USER = {"id": "usr_harbourside", "name": "Jo Park", "email": "jo@harbourside-clinic.test"}
# The account for showing the app in the Cognizant BrAInstorm Challenge 2026 (the video, the
# judges, the final pitch) and for nothing else; the email says so on the sign-in page. It is in
# Sam's clinic, so it sees the same cases, and uses the same SEED_PASSWORD.
DEMO_ORG_ID = "org_demo"
DEMO_USER = {
    "id": "usr_brainstorm2026_demo",
    "name": "BrAInstorm 2026 demo",
    "email": "brainstorm2026-demo-only@example-clinic.test",
}
CHILD_TABLES = (
    LoginSessionRow, PackageRow, IntakeTokenRow, ActivityRow, FormFieldRow, LegalSourceRow,
    GapRow, TimelineEventRow, ExcerptTranslationRow,
    RunRow, EvidenceRow, CaseRow, UserRow, OrgRow,
)
RUN_STEPS = [
    ("ingest", "Check files"), ("extract", "Extract facts"), ("timeline", "Build timeline"),
    ("rules", "Look up official rules"), ("gaps", "Find missing information"),
    ("draft", "Draft form"),
]


def dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def dt_req(value: str) -> datetime:
    parsed = dt(value)
    assert parsed is not None
    return parsed


def shift_day(value: str, delta: int) -> str:
    return (date.fromisoformat(value[:10]) + timedelta(days=delta)).isoformat()


def shift_label(label: str, delta: int) -> str:
    # Labels embed a date: "Must be received by · hearing 2026-11-12"
    head, _, tail = label.rpartition(" ")
    return f"{head} {shift_day(tail, delta)}" if tail[:2] == "20" else label


def add_placeholder_rows(session: Session, c: dict) -> None:
    """Other cases only have summary data in the prototype. Create minimal rows so the
    open_blocking / pending_review counts (derived from rows) match the summary."""
    session.flush()
    for i in range(c["open_blocking"]):
        session.add(GapRow(
            id=f"{c['id']}_gap_{i + 1}", case_id=c["id"], position=i, severity="blocking",
            title="[Synthetic placeholder] Required item",
            why="Placeholder row so this summary-only case has real counts.", how_to_fix="",
            status="open",
        ))
    for i in range(c["pending_review"]):
        session.add(TimelineEventRow(
            id=f"{c['id']}_te_{i + 1}", case_id=c["id"], date={
                "value": None, "precision": "unknown", "basis": "user"},
            sort_key="9", title="[Synthetic placeholder] Event to review",
            description="Placeholder row so this summary-only case has real counts.",
            actor="other", strength="needs_support", review_status="pending",
        ))


def seed(session: Session) -> None:
    settings = get_settings()
    raw = json.loads(DATA.read_text(encoding="utf-8"))  # not the Windows ANSI code page
    s, org, me = raw["SEED"], raw["ORG"], raw["ME"]
    delta = (date.today() - date.fromisoformat(raw["dumped_on"])).days

    for table in CHILD_TABLES:
        session.exec(delete(table))  # type: ignore[call-overload]

    if not settings.seed_password:
        raise SystemExit("SEED_PASSWORD is required to seed the caseworker accounts")
    password_hash = hash_password(settings.seed_password)
    session.add(OrgRow(id=org["id"], name=org["name"]))
    session.add(UserRow(id=me["id"], org_id=org["id"], name=me["name"],
                        email=CASEWORKER_EMAIL, password_hash=password_hash))
    session.add(OrgRow(**OTHER_ORG))
    session.add(UserRow(org_id=OTHER_ORG["id"], password_hash=password_hash, **OTHER_USER))
    session.flush()
    ensure_demo_account(session, password_hash)

    def add_case(c: dict, *, demo: bool) -> CaseRow:
        deadline = c.get("next_deadline")
        if deadline:
            # `demo_offset_days` keeps the deadline relative to today when it is read (see
            # `dates.demo_deadline`), so a database seeded days ago never shows a past deadline.
            dumped = date.fromisoformat(raw["dumped_on"])
            offset = (date.fromisoformat(deadline["date"]) - dumped).days
            deadline = {"date": shift_day(deadline["date"], delta),
                        "label": shift_label(deadline["label"], delta),
                        "demo_offset_days": offset}
        intake = c.get("intake")
        row = CaseRow(
            id=c["id"], org_id=org["id"], assigned_to_id=me["id"], title=c["title"], unit=c["unit"],
            tenant_name=c["tenant"]["name"] if demo else c["tenant_name"],
            tenant_language=c["tenant"]["preferred_language"] if demo else c["tenant_language"],
            stage=c.get("stage", "needs_review"), next_deadline=deadline,
            has_demo_data=demo,
            intake_status=intake["status"] if intake else "sent",
            intake_sent_at=dt(intake["sent_at"]) if intake else None,
            intake_last_upload_at=dt(intake["last_upload_at"]) if intake else None,
            tenant_attested_genuine=intake["tenant_attested_genuine"] if intake else False,
            tenant_attested_at=dt(intake["tenant_attested_at"]) if intake else None,
            created_at=dt_req(c["created_at"] if demo else c["updated_at"]),
            updated_at=datetime.now(UTC) if demo else dt_req(c["updated_at"]),
            form_name=FORM_J_DRAFT_LABEL if demo else "",
            form_disclaimer=(
                "Prepared with the tenant to help fill in the official form. It is information, "
                "not legal advice, and is not submitted anywhere." if demo else ""
            ),
        )
        session.add(row)
        return row

    demo_case = s["case"]
    demo_case["stage"] = "needs_review"
    add_case(demo_case, demo=True)
    live_case = s["liveCase"]  # synthetic, empty: the live demo uploads its evidence
    add_case(live_case, demo=True)
    for other in s["otherCases"]:
        other["updated_at"] = shift_day(other["updated_at"], delta) + other["updated_at"][10:]
        add_case(other, demo=False)
        if other.get("synthetic_placeholder_rows"):
            add_placeholder_rows(session, other)
    session.flush()
    case_id = demo_case["id"]

    for i, e in enumerate(s["evidence"]):
        session.add(EvidenceRow(
            id=e["id"], case_id=case_id, position=i, label=e["label"], filename=e["filename"],
            kind=e["kind"], mime=e["mime"], size_bytes=e["size_bytes"], sha256=e["sha256"],
            received_at=dt(e["received_at"]), uploaded_by=e["uploaded_by"],  # type: ignore[arg-type]
            format_check=e["format_check"], metadata_=e["metadata"],
            metadata_stripped_for_ai=e["metadata_stripped_for_ai"], processing=e["processing"],
            error=e["error"],
        ))
    for t in s["timeline"]:
        session.add(TimelineEventRow(
            id=t["id"], case_id=case_id, date=t["date"], sort_key=t["date"]["value"] or "9",
            title=t["title"], description=t["description"], actor=t["actor"], sources=t["sources"],
            strength=t["strength"], strength_reasons=t["strength_reasons"], flags=t["flags"],
            review_status=t["review_status"], caseworker_note=t["caseworker_note"],
            reviewed_by=t["reviewed_by"],
        ))
    for i, g in enumerate(s["gaps"]):
        session.add(GapRow(
            id=g["id"], case_id=case_id, position=i, severity=g["severity"], title=g["title"],
            why=g["why"], how_to_fix=g["how_to_fix"], source_id=g["source_id"], input=g["input"],
            tenant_request=g["tenant_request"], status=g["status"],
            resolution_note=g["resolution_note"],
        ))
    for i, src in enumerate(s["sources"]):
        session.add(LegalSourceRow(
            id=src["id"], case_id=case_id, position=i, title=src["title"], kind=src["kind"],
            url=src["url"], last_verified_at=dt(src["last_verified_at"]),
            verify_status=src["verify_status"], used_for=src["used_for"],
        ))
    for i, f in enumerate(s["fields"]):
        session.add(FormFieldRow(
            case_id=case_id, key=f["key"], position=i, label=f["label"], value=f["value"],
            required=f["required"], status=f["status"], sources=f["sources"],
            note=f.get("note"),
        ))
    for a in reversed(s["activity"]):  # dump is newest-first; insert oldest first
        session.add(ActivityRow(case_id=case_id, at=dt(a["at"]), actor=a["actor"],  # type: ignore[arg-type]
                                actor_name=a["actor_name"], action=a["action"]))

    steps = [{"key": k, "label": lbl, "status": "done", "summary": None, "tool_calls": []}
             for k, lbl in RUN_STEPS]
    steps[4]["status"] = "needs_input"
    session.add(RunRow(id="run_001", case_id=case_id, status="needs_review",
                       started_at=dt("2026-10-28T14:08:02-03:00"),  # type: ignore[arg-type]
                       finished_at=dt("2026-10-28T14:09:31-03:00"), steps=steps))
    link = issue_token(session, case_id, settings.intake_token_ttl_days)
    session.commit()
    print(f"demo intake link (shown once): {settings.public_base_url}/i/{link}")
    live_link = issue_token(session, live_case["id"], settings.intake_token_ttl_days)
    session.commit()
    print(f"live intake link (shown once): {settings.public_base_url}/i/{live_link}")


def is_empty(session: Session) -> bool:
    return session.exec(select(OrgRow)).first() is None


def ensure_demo_account(session: Session, password_hash: str) -> bool:
    """Add the BrAInstorm 2026 demo account when it is missing and its clinic exists. Returns
    whether it was added. Never changes an account that is already there. Caller commits."""
    if session.get(UserRow, DEMO_USER["id"]) is not None:
        return False
    if session.get(OrgRow, DEMO_ORG_ID) is None:
        return False
    session.add(UserRow(org_id=DEMO_ORG_ID, password_hash=password_hash, **DEMO_USER))
    session.flush()
    return True


def main(argv: list[str]) -> None:
    """Default: seed only an empty database, so restarting never wipes work. A database that is
    already seeded only gets the demo account if it lacks it.
    --force wipes and reloads (make seed / make reset)."""
    force = "--force" in argv
    with Session(get_engine()) as db_session:
        if not force and not is_empty(db_session):
            print("database already has data: not seeding (use --force or `make reset` to wipe)")
            password = get_settings().seed_password
            if password and ensure_demo_account(db_session, hash_password(password)):
                db_session.commit()
                print(f"added the demo account {DEMO_USER['email']}")
            return
        seed(db_session)
    print("seeded")


if __name__ == "__main__":
    main(sys.argv[1:])
