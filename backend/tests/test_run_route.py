"""POST /cases/{id}/runs: starts the runner in the background with the database text store and
the configured provider. Tests use FixtureProvider only, so nothing reaches the network."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, delete, select

from app.config import get_settings
from app.main import api
from app.models import ActivityRow, EvidencePageRow, RunRow, TimelineEventRow
from app.routers import runs as runs_router
from app.services.agent import llm
from tests.agent_support import CASE, LEASE, thinking, tool_use, turn

pytestmark = pytest.mark.usefixtures("hearing_known")

URL = f"/api/cases/{CASE}/runs"
FIRST = {"value": "2026-05-01", "precision": "day", "basis": "document_text"}


def script() -> dict[str, llm.LLMResponse]:
    return {
        "timeline-turn1": turn(thinking("look"), tool_use("list_evidence", "u1")),
        "timeline-turn2": turn(tool_use("read_evidence", "u2", evidence_id="ev_01")),
        "timeline-turn3": turn(tool_use(
            "propose_event", "u3", title="Tenancy starts", description="The lease begins.",
            actor="other", date_candidates=[FIRST],
            sources=[{"evidence_id": "ev_01", "locator": "page 1", "excerpt": LEASE}],
        )),
        "timeline-turn4": turn(tool_use("finish", "u4", summary="One event proposed.")),
        # the gaps step runs after the timeline in a full run (WS4)
        "gaps-turn1": turn(tool_use("finish", "u5", summary="Nothing new to add.")),
        # and the draft step after it (#40)
        "draft-turn1": turn(tool_use("finish", "u6", summary="Nothing to fill.")),
    }


@pytest.fixture
def provider() -> Iterator[None]:
    """The route's provider dependency, replaced by a scripted FixtureProvider."""
    api.dependency_overrides[runs_router.provider_dep] = (
        lambda: llm.FixtureProvider(scripted=script())
    )
    yield
    api.dependency_overrides.pop(runs_router.provider_dep, None)


@pytest.fixture
def pages(session: Session) -> Iterator[None]:
    session.add(EvidencePageRow(evidence_id="ev_01", locator="page 1", case_id=CASE,
                                position=0, text=LEASE))
    session.commit()
    yield
    session.exec(delete(EvidencePageRow))  # type: ignore[call-overload]
    session.commit()


def agent_events(session: Session) -> list[TimelineEventRow]:
    session.expire_all()
    rows = session.exec(select(TimelineEventRow).where(TimelineEventRow.case_id == CASE)).all()
    return [e for e in rows if len(e.id) == 13]  # seeded ids are te_01..


def test_post_returns_the_run_at_once_and_it_finishes_in_the_background(
    fresh_db, client, session, provider, pages
):
    r = client.post(URL)
    assert r.status_code == 200
    started = r.json()
    assert started["status"] == "running" and started["case_id"] == CASE
    assert started["id"].startswith("run_") and started["finished_at"] is None

    done = client.get(f"/api/runs/{started['id']}").json()
    assert done["status"] == "needs_review"  # the pending event waits for the caseworker
    timeline = next(s for s in done["steps"] if s["key"] == "timeline")
    assert timeline["status"] == "done"
    assert [e.title for e in agent_events(session)] == ["Tenancy starts"]
    assert client.get(f"/api/cases/{CASE}/runs/latest").json()["id"] == started["id"]


def test_it_logs_the_start_and_the_finish(fresh_db, client, session, provider, pages):
    run_id = client.post(URL).json()["id"]
    session.expire_all()
    lines = [a.action for a in session.exec(
        select(ActivityRow).where(ActivityRow.case_id == CASE)).all()]
    assert f"Started {run_id}" in lines
    assert any(line.startswith(f"Finished {run_id}") for line in lines)


def test_the_text_comes_from_the_database_store(fresh_db, client, session, provider):
    """No page rows: the lease cannot be read, so the cited excerpt is rejected and no event
    is added. The route reads the evidence_page table, not a fixture."""
    run_id = client.post(URL).json()["id"]
    assert agent_events(session) == []
    done = client.get(f"/api/runs/{run_id}").json()
    assert done["status"] != "running"


def test_scope_comes_from_the_path_never_the_body(fresh_db, client, session, provider, pages):
    body = {"case_id": "case_002", "org_id": "org_harbourside", "case": "case_002"}
    run = client.post(URL, json=body).json()
    assert run["case_id"] == CASE
    session.expire_all()
    assert session.get(RunRow, run["id"]).case_id == CASE  # type: ignore[union-attr]


def test_another_orgs_caseworker_gets_404_and_starts_nothing(
    fresh_db, other_org_client, session, provider
):
    before = len(session.exec(select(RunRow)).all())
    assert other_org_client.post(URL).status_code == 404
    session.expire_all()
    assert len(session.exec(select(RunRow)).all()) == before


def test_unknown_case_is_404_and_signed_out_is_401(fresh_db, client, anon_client, provider):
    assert client.post("/api/cases/case_nope/runs").status_code == 404
    assert anon_client.post(URL).status_code == 401


def test_a_second_start_while_one_is_running_is_409(fresh_db, client, session, provider):
    session.add(RunRow(id="run_busy", case_id=CASE, status="running",
                       started_at=datetime.now(UTC), steps=[]))
    session.commit()
    r = client.post(URL)
    assert r.status_code == 409
    assert r.json()["detail"]["reasons"] == ["An analysis is already running for this case"]


def test_a_run_inside_its_budget_still_blocks_a_new_one(
    fresh_db, client, session, provider, monkeypatch
):
    """Two minutes in: past the old 60 s budget, but inside the 180 s default (#49)."""
    monkeypatch.setattr(get_settings(), "run_budget_s", 180.0)
    started = datetime.now(UTC) - timedelta(seconds=120)
    session.add(RunRow(id="run_slow", case_id=CASE, status="running", started_at=started, steps=[]))
    session.commit()
    assert client.post(URL).status_code == 409


def test_the_stale_cutoff_follows_the_run_budget(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "run_budget_s", 300.0)
    assert runs_router.stale_after() == timedelta(seconds=330)


def test_a_run_cut_off_long_ago_does_not_block_a_new_one(
    fresh_db, client, session, provider, pages
):
    old = datetime.now(UTC) - runs_router.stale_after() - timedelta(seconds=5)
    session.add(RunRow(id="run_stale", case_id=CASE, status="running", started_at=old, steps=[]))
    session.commit()
    assert client.post(URL).status_code == 200


def test_live_mode_without_a_key_is_503_and_starts_nothing(
    fresh_db, client, session, monkeypatch
):
    monkeypatch.setattr(get_settings(), "llm_mode", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    before = len(session.exec(select(RunRow)).all())
    r = client.post(URL)
    assert r.status_code == 503
    assert "ANTHROPIC" not in r.text  # the response doesn't describe server configuration
    session.expire_all()
    assert len(session.exec(select(RunRow)).all()) == before


def test_a_provider_with_no_response_fails_the_run_instead_of_leaving_it_running(
    fresh_db, client, session, pages
):
    api.dependency_overrides[runs_router.provider_dep] = (
        lambda: llm.FixtureProvider(scripted={})  # no fixture for "timeline-turn1"
    )
    try:
        run_id = client.post(URL).json()["id"]
    finally:
        api.dependency_overrides.pop(runs_router.provider_dep, None)
    done = client.get(f"/api/runs/{run_id}").json()
    assert done["status"] == "failed" and done["finished_at"] is not None
