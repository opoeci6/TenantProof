"""The draft step (#40): the form agent fills the fields still missing from the evidence. Every
value is copied from a citation the verifier accepts and waits for the caseworker as
`needs_confirmation`. Fixture providers only, so nothing reaches the network."""

import asyncio

import pytest
from sqlmodel import Session, col, select

from app.models import ActivityRow, CaseRow, EvidenceRow, FormFieldRow, GapRow, RunRow
from app.services.agent import prompts, runner
from app.services.agent import tools as t
from app.services.legal import gap_checks, gaps
from tests.agent_support import (
    CASE,
    LEASE,
    OTHER_ORG,
    RecordingProvider,
    call,
    demo_store,
    make_ctx,
    run,
    tool_use,
    turn,
)

LIVE = "case_live_001"  # seeded empty: no evidence, no form
NAME_SRC = {"evidence_id": "ev_01", "locator": "page 2", "excerpt": "Landlord: Harbour Ltd."}
LEASE_SRC = {"evidence_id": "ev_01", "locator": "page 1", "excerpt": LEASE}


def field(session: Session, key: str, case_id: str = CASE) -> FormFieldRow:
    session.expire_all()
    row = session.get(FormFieldRow, (case_id, key))
    assert row is not None
    return row


def clear(session: Session, key: str) -> None:
    """Make a seeded field missing again, as if nothing had filled it."""
    row = field(session, key)
    row.value, row.status, row.sources, row.note = None, "missing", [], None
    session.add(row)
    session.commit()


def fill(session: Session, **raw):
    result = call("fill_form_field", make_ctx(session), raw)
    session.commit()
    return result


# --- the tool ---------------------------------------------------------------------------------


def test_a_value_from_a_verified_citation_is_saved_for_confirmation(fresh_db, session):
    clear(session, "landlord_name")
    out = fill(session, field_key="landlord_name", value="harbour ltd.", sources=[NAME_SRC])
    assert isinstance(out, t.FillFormFieldOutput) and out.filled
    row = field(session, "landlord_name")
    assert row.status == "needs_confirmation" and row.value == "harbour ltd."
    # the saved source is the text found in the evidence, not the model's string
    assert row.sources == [
        {"evidence_id": "ev_01", "locator": "page 2", "excerpt": "Landlord: Harbour Ltd."}
    ]
    assert row.note == t.DRAFTED_NOTE
    logged = session.exec(select(ActivityRow).where(ActivityRow.case_id == CASE)).all()
    assert any("Drafted form field “Landlord name”" in a.action for a in logged)


def test_the_value_must_be_words_the_cited_excerpt_has(fresh_db, session):
    """A real citation next to an invented value proves nothing: the value is refused."""
    clear(session, "landlord_name")
    out = fill(session, field_key="landlord_name", value="Harbour Holdings", sources=[NAME_SRC])
    assert isinstance(out, t.ToolError) and "word for word" in out.message
    assert field(session, "landlord_name").status == "missing"


def test_a_date_is_kept_as_the_evidence_writes_it_not_converted(fresh_db, session):
    clear(session, "tenancy_start")
    converted = fill(session, field_key="tenancy_start", value="2026-05-01", sources=[LEASE_SRC])
    assert isinstance(converted, t.ToolError)
    out = fill(session, field_key="tenancy_start", value="May 1, 2026", sources=[LEASE_SRC])
    assert isinstance(out, t.FillFormFieldOutput)
    assert field(session, "tenancy_start").value == "May 1, 2026"


def test_a_month_only_value_is_saved_as_written(fresh_db, session):
    """A month-precision date stays a month: nothing invents the day."""
    clear(session, "tenancy_start")
    caption = {"evidence_id": "ev_06", "locator": "photo 1",
               "excerpt": "mould on the bathroom ceiling, taken September 2026"}  # fmt: skip
    out = fill(session, field_key="tenancy_start", value="September 2026", sources=[caption])
    assert isinstance(out, t.FillFormFieldOutput)
    assert field(session, "tenancy_start").value == "September 2026"


def test_a_fragment_of_a_word_or_number_is_not_a_value(fresh_db, session):
    """"15" is in "$1,150" and "e" is in "Rent": neither is what the lease says the rent is."""
    clear(session, "monthly_rent")
    for fragment in ("15", "150", "e", "Ren", ",", "$", "150 per", "$1"):
        out = fill(session, field_key="monthly_rent", value=fragment, sources=[LEASE_SRC])
        assert isinstance(out, t.ToolError), fragment
    assert field(session, "monthly_rent").status == "missing"
    out = fill(session, field_key="monthly_rent", value="$1,150", sources=[LEASE_SRC])
    assert isinstance(out, t.FillFormFieldOutput)
    assert field(session, "monthly_rent").value == "$1,150"


def test_a_blank_value_and_no_sources_are_refused(fresh_db, session):
    clear(session, "landlord_name")
    blank = fill(session, field_key="landlord_name", value="   ", sources=[NAME_SRC])
    assert isinstance(blank, t.ToolError)
    unsourced = fill(session, field_key="landlord_name", value="Harbour Ltd.", sources=[])
    assert isinstance(unsourced, t.ToolError) and unsourced.kind == "invalid_input"
    assert field(session, "landlord_name").status == "missing"


def test_an_invented_excerpt_saves_nothing(fresh_db, session):
    clear(session, "landlord_name")
    fake = {**NAME_SRC, "excerpt": "Landlord: Harbour Holdings International"}
    out = fill(session, field_key="landlord_name", value="Harbour Holdings", sources=[fake])
    assert isinstance(out, t.ToolError) and out.problems and "source 1" in out.problems[0]
    assert field(session, "landlord_name").status == "missing"


def test_another_cases_evidence_cannot_be_cited(fresh_db, session):
    """A run on the live case cites the demo case's lease, which the text store does hold."""
    live = session.get(CaseRow, LIVE)
    assert live is not None and session.get(EvidenceRow, "ev_01") is not None
    gaps.ensure_form_fields(session, live)
    session.commit()
    out = call(
        "fill_form_field", make_ctx(session, case_id=LIVE),
        {"field_key": "landlord_name", "value": "Harbour Ltd.", "sources": [NAME_SRC]},
    )  # fmt: skip
    session.commit()
    assert isinstance(out, t.ToolError)
    assert "evidence not found in this case" in out.problems[0]
    assert field(session, "landlord_name", LIVE).status == "missing"


def test_the_remedy_is_never_drafted(fresh_db, session):
    out = fill(session, field_key="remedy", value="Harbour Ltd.", sources=[NAME_SRC])
    assert isinstance(out, t.ToolError) and "tenant's choice" in out.message
    assert field(session, "remedy").status == "missing" and field(session, "remedy").value is None


def test_a_filled_field_is_never_overwritten(fresh_db, session):
    """user_entered (the caseworker's), needs_confirmation (waiting) and auto_filled all stay."""
    row = field(session, "landlord_name")
    for status in ("user_entered", "needs_confirmation", "auto_filled"):
        row = field(session, "landlord_name")
        row.status, row.value = status, "Kept Ltd."
        session.add(row)
        session.commit()
        out = fill(session, field_key="landlord_name", value="Harbour Ltd.", sources=[NAME_SRC])
        assert isinstance(out, t.ToolError) and "already has a value" in out.message
        assert field(session, "landlord_name").value == "Kept Ltd."


def test_an_unknown_field_and_another_org_are_refused(fresh_db, session):
    out = fill(session, field_key="pet_policy", value="Harbour Ltd.", sources=[NAME_SRC])
    assert isinstance(out, t.ToolError) and out.kind == "invalid_input"
    clear(session, "landlord_name")
    foreign = call(
        "fill_form_field", make_ctx(session, org_id=OTHER_ORG),
        {"field_key": "landlord_name", "value": "Harbour Ltd.", "sources": [NAME_SRC]},
    )  # fmt: skip
    assert isinstance(foreign, t.ToolError) and foreign.kind == "not_found"
    assert field(session, "landlord_name").status == "missing"


def test_the_input_takes_no_scope_fields(fresh_db, session):
    clear(session, "landlord_name")
    out = fill(
        session, field_key="landlord_name", value="Harbour Ltd.", sources=[NAME_SRC],
        case_id="case_002",
    )  # fmt: skip
    assert isinstance(out, t.ToolError) and out.kind == "invalid_input"


# --- confirming in the form ---------------------------------------------------------------------


def test_confirming_keeps_the_sources_and_clears_the_agents_note(fresh_db, session, client):
    clear(session, "landlord_name")
    fill(session, field_key="landlord_name", value="Harbour Ltd.", sources=[NAME_SRC])
    r = client.patch(
        f"/api/cases/{CASE}/form-draft/fields/landlord_name", json={"value": "Harbour Ltd."}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "user_entered" and body["note"] is None
    assert body["sources"][0]["excerpt"] == "Landlord: Harbour Ltd."


def test_changing_the_drafted_value_drops_its_sources(fresh_db, session, client):
    clear(session, "landlord_name")
    fill(session, field_key="landlord_name", value="Harbour Ltd.", sources=[NAME_SRC])
    r = client.patch(
        f"/api/cases/{CASE}/form-draft/fields/landlord_name", json={"value": "Other Co."}
    )
    assert r.status_code == 200 and r.json()["sources"] == []


def test_confirming_resolves_the_gap_for_that_field(fresh_db, session, client):
    gap = session.exec(
        select(GapRow).where(GapRow.case_id == CASE, GapRow.status == "open")
    ).all()
    keyed = next(g for g in gap if (g.input or {}).get("field_key") == "landlord_address")
    row = field(session, "landlord_address")
    row.status, row.value, row.note = "needs_confirmation", "1 Harbour Rd.", t.DRAFTED_NOTE
    session.add(row)
    session.commit()
    r = client.patch(
        f"/api/cases/{CASE}/form-draft/fields/landlord_address", json={"value": "1 Harbour Rd."}
    )
    assert r.status_code == 200
    session.expire_all()
    resolved = session.get(GapRow, keyed.id)
    assert resolved is not None and resolved.status == "resolved"


def address_gap(session: Session) -> GapRow:
    gaps_open = session.exec(select(GapRow).where(GapRow.case_id == CASE)).all()
    return next(g for g in gaps_open if (g.input or {}).get("field_key") == "landlord_address")


def draft_address(session: Session) -> None:
    """landlord_address as the agent leaves it: drafted, cited, its field gap still open."""
    row = field(session, "landlord_address")
    row.status, row.value, row.note = "needs_confirmation", "Harbour Ltd.", t.DRAFTED_NOTE
    row.sources = [{"evidence_id": "ev_01", "locator": "page 2",
                    "excerpt": "Landlord: Harbour Ltd."}]  # fmt: skip
    session.add(row)
    session.commit()


def test_another_value_saved_on_the_gap_page_drops_the_agents_note_and_quote(
    fresh_db, session, client
):
    """The Missing info page writes the same field: its value must not sit next to a quote that
    says something else, or a note asking to confirm a value that is gone."""
    draft_address(session)
    gap = address_gap(session)
    r = client.patch(f"/api/gaps/{gap.id}", json={"status": "resolved", "value": "22 Other St."})
    assert r.status_code == 200
    row = field(session, "landlord_address")
    assert (row.status, row.value, row.note, row.sources) == (
        "user_entered", "22 Other St.", None, []
    )
    # Reopening the item empties the field, and no quote is left next to "Missing".
    r = client.patch(f"/api/gaps/{gap.id}", json={"status": "open"})
    assert r.status_code == 200
    row = field(session, "landlord_address")
    assert (row.status, row.value, row.sources) == ("missing", None, [])


def test_a_new_value_keeps_the_source_that_still_supports_it(fresh_db, session, client):
    """The seeded rent cites two amounts; choosing $1,700 keeps the addendum's quote."""
    r = client.patch(f"/api/cases/{CASE}/form-draft/fields/monthly_rent", json={"value": "$1,700"})
    assert r.status_code == 200
    excerpts = [s["excerpt"] for s in r.json()["sources"]]
    assert excerpts == ["Rent adjusted to $1,700 effective Sept 1"]
    assert r.json()["note"] is not None  # a note the agent did not write is left alone


def test_a_draft_the_caseworker_cleared_is_not_drafted_again(fresh_db, session, client):
    draft_address(session)
    r = client.patch(f"/api/cases/{CASE}/form-draft/fields/landlord_address", json={"value": ""})
    assert r.status_code == 200 and r.json()["status"] == "missing"
    assert r.json()["note"] == t.CLEARED_NOTE and r.json()["sources"] == []
    out = fill(session, field_key="landlord_address", value="Harbour Ltd.", sources=[NAME_SRC])
    assert isinstance(out, t.ToolError) and "cleared" in out.message
    done, provider = run_draft(session, {})  # remedy and the cleared field: nothing to fill
    assert draft_step(done)["summary"] == "No missing form fields to fill."
    assert provider.requests == []


# --- the step in a run --------------------------------------------------------------------------


def run_draft(session: Session, script, case_id: str = CASE) -> tuple[RunRow, RecordingProvider]:
    case = session.get(CaseRow, case_id)
    assert case is not None
    row = runner.create_run(session, case)
    provider = RecordingProvider(script)
    done = run(runner.execute_run(session, row, provider, demo_store(), steps=("draft",)))
    return done, provider


def draft_step(row: RunRow) -> dict:
    return next(s for s in row.steps if s["key"] == "draft")


def test_the_step_fills_from_the_evidence_and_says_what_it_did(fresh_db, session):
    clear(session, "landlord_name")
    done, provider = run_draft(session, {
        "draft-turn1": turn(tool_use("read_evidence", "d1", evidence_id="ev_01", page=2)),
        "draft-turn2": turn(tool_use(
            "fill_form_field", "d2", field_key="landlord_name", value="Harbour Ltd.",
            sources=[NAME_SRC],
        )),
        "draft-turn3": turn(tool_use(
            "fill_form_field", "d3", field_key="landlord_address", value="Harbour Ltd.",
            sources=[{**NAME_SRC, "excerpt": "nothing like this is in the lease"}],
        )),
        "draft-turn4": turn(tool_use("finish", "d4", summary="Filled the landlord name.")),
    })  # fmt: skip
    step = draft_step(done)
    assert step["status"] == "done"
    assert step["summary"] == "Filled the landlord name. (1 fields filled, 1 attempts rejected)"
    assert field(session, "landlord_name").status == "needs_confirmation"
    assert field(session, "landlord_address").status == "missing"
    # the kickoff lists the missing fields the agent may fill, never the remedy
    kickoff = provider.requests[0].messages[0].content
    assert isinstance(kickoff, str)
    assert "landlord_name: Landlord name" in kickoff and "landlord_address" in kickoff
    assert "remedy" not in kickoff
    assert provider.requests[0].tools == t.tool_specs(prompts.DRAFT_TOOLS)


def test_the_step_has_one_write_tool_and_cannot_pause() -> None:
    effects = {n: t.TOOLS[n].effect for n in prompts.DRAFT_TOOLS}
    assert [n for n, e in effects.items() if e == "draft"] == ["fill_form_field"]
    assert set(effects) - {"fill_form_field", "finish"} == {
        n for n, e in effects.items() if e == "read"
    }
    assert "ask_caseworker" not in prompts.DRAFT_TOOLS


def test_nothing_to_fill_means_no_model_call(fresh_db, session):
    """Only the remedy is missing: the step is done without asking the model anything."""
    address = field(session, "landlord_address")
    address.status, address.value = "user_entered", "1 Harbour Rd."
    session.add(address)
    session.commit()
    done, provider = run_draft(session, {})
    step = draft_step(done)
    assert step["status"] == "done" and step["summary"] == "No missing form fields to fill."
    assert provider.requests == []
    assert field(session, "remedy").status == "missing"


def test_a_case_with_no_form_and_no_evidence_gets_the_form_and_no_model_call(fresh_db, session):
    """Zero evidence and zero facts: the form is created, nothing is asked, every field stays
    missing."""
    done, provider = run_draft(session, {}, case_id=LIVE)
    step = draft_step(done)
    assert (step["status"], step["summary"]) == (
        "done", "No evidence text to fill the form from yet."
    )
    stmt = select(FormFieldRow).where(FormFieldRow.case_id == LIVE)
    rows = session.exec(stmt.order_by(col(FormFieldRow.position))).all()
    assert len(rows) == 7 and {r.status for r in rows} == {"missing"}
    assert provider.requests == []


class SlowProvider(RecordingProvider):
    def __init__(self, scripted, delay: float) -> None:
        super().__init__(scripted)
        self.delay = delay

    async def complete(self, request):
        await asyncio.sleep(self.delay)
        return await super().complete(request)


class Boom(RecordingProvider):
    """Answers the scripted turns, then raises instead of a response (a killed worker)."""

    async def complete(self, request):
        if request.fixture_key not in self.scripted:
            raise asyncio.CancelledError
        return await super().complete(request)


FILL_NAME = tool_use(
    "fill_form_field", "d1", field_key="landlord_name", value="Harbour Ltd.", sources=[NAME_SRC]
)


def test_a_deadline_in_the_draft_step_reports_its_own_counts(fresh_db, session):
    clear(session, "landlord_name")
    case = session.get(CaseRow, CASE)
    assert case is not None
    provider = SlowProvider({
        "draft-turn1": turn(FILL_NAME),
        "draft-turn2": turn(tool_use("finish", "d2", summary="never reached")),
    }, delay=0.25)  # fmt: skip
    row = runner.create_run(session, case)
    done = run(runner.execute_run(
        session, row, provider, demo_store(), steps=("draft",), deadline_s=0.4))
    step = draft_step(done)
    assert step["status"] == "done" and "deadline" in step["summary"]
    assert "1 fields filled, 0 attempts rejected" in step["summary"]
    assert field(session, "landlord_name").status == "needs_confirmation"


def test_a_killed_worker_resumes_the_draft_step_from_its_checkpoint(fresh_db, session):
    """Once every field is filled a fresh start would make no model call; a resume must carry
    on with the saved conversation instead, keeping what it did."""
    clear(session, "landlord_name")
    address = field(session, "landlord_address")
    address.status, address.value = "user_entered", "1 Harbour Rd."
    session.add(address)
    session.commit()
    case = session.get(CaseRow, CASE)
    assert case is not None
    row = runner.create_run(session, case)
    with pytest.raises(asyncio.CancelledError):
        run(runner.execute_run(
            session, row, Boom({"draft-turn1": turn(FILL_NAME)}), demo_store(),
            steps=("draft",),
        ))  # fmt: skip
    session.expire_all()
    crashed = session.get(RunRow, row.id)
    assert crashed is not None
    assert draft_step(crashed)["status"] == "running"
    assert field(session, "landlord_name").status == "needs_confirmation"  # nothing left to fill

    resumed = RecordingProvider({"draft-turn2": turn(tool_use("finish", "d2", summary="Done."))})
    done = run(runner.execute_run(session, crashed, resumed, demo_store(), steps=("draft",)))
    assert [r.fixture_key for r in resumed.requests] == ["draft-turn2"]
    step = draft_step(done)
    assert step["summary"] == "Done. (1 fields filled, 0 attempts rejected)"
    assert [c["tool"] for c in step["tool_calls"]] == ["fill_form_field", "finish"]


def test_a_rerun_after_confirmation_leaves_the_caseworkers_value(fresh_db, session, client):
    clear(session, "landlord_name")
    fill(session, field_key="landlord_name", value="Harbour Ltd.", sources=[NAME_SRC])
    client.patch(f"/api/cases/{CASE}/form-draft/fields/landlord_name", json={"value": "HL Ltd."})
    done, provider = run_draft(session, {
        "draft-turn1": turn(tool_use(
            "fill_form_field", "d1", field_key="landlord_name", value="Harbour Ltd.",
            sources=[NAME_SRC],
        )),
        "draft-turn2": turn(tool_use("finish", "d2", summary="Tried.")),
    })  # fmt: skip
    kickoff = provider.requests[0].messages[0].content
    assert isinstance(kickoff, str) and "landlord_name" not in kickoff
    row = field(session, "landlord_name")
    assert (row.status, row.value) == ("user_entered", "HL Ltd.")
    assert draft_step(done)["summary"] == "Tried. (0 fields filled, 1 attempts rejected)"


def test_draft_runs_before_gaps_so_a_filled_field_gets_no_missing_gap(fresh_db, session):
    """The gaps step's missing-field check runs after the form agent: a field it drafted from the
    evidence waits for the caseworker, and the tenant is never asked for it."""
    order = list(runner.IMPLEMENTED_STEPS)
    assert order.index("draft") < order.index("gaps")
    assert list(runner.STEP_LABELS).index("draft") < list(runner.STEP_LABELS).index("gaps")
    live = session.get(CaseRow, LIVE)
    assert live is not None
    gaps.ensure_form_fields(session, live)
    row = field(session, "landlord_name", LIVE)
    row.status, row.value = "needs_confirmation", "Harbour Ltd."
    session.add(row)
    session.commit()
    keys = {spec.field_key for spec in gap_checks.specs(gaps.case_facts(session, live))}
    assert "landlord_name" not in keys and "landlord_address" in keys
