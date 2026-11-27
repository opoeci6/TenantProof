"""Issue #60 end to end: with PRIVACY_PLACEHOLDERS on, the model gets placeholders instead of the
seeded demo case's personal details, and what it proposes comes back with the real values and
passes the same quote check. Scripted FixtureProvider only; no network."""

import json
from typing import Any

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.models import (
    ActivityRow,
    CaseRow,
    FormFieldRow,
    PrivacyTermRow,
    RunRow,
    TimelineEventRow,
)
from app.services.agent import llm, placeholders, runner
from app.services.agent.evidence_text import InMemoryTextStore, Page
from tests.agent_support import (
    CASE,
    MSG1,
    RecordingProvider,
    demo_store,
    run,
    thinking,
    tool_use,
    turn,
)

pytestmark = pytest.mark.usefixtures("hearing_known")

N = "0123456789abcdef"
LETTER = (
    "To: Jordan Lee, Unit 4B, 100 Example Street, Halifax, NS. From the landlord at "
    "22 Placeholder Road. Call 902-555-0187 or email jordan.lee@example.test.\nSigned,\nJordan\nLee"
)  # the signature splits the name over two lines: JSON writes it as Jordan\nLee
# What the seeded case calls its tenant and unit, as they appear in LETTER and MSG1.
PERSONAL = ("Jordan", "Lee", "Example Street", "Unit 4B", "Placeholder Road", "902-555-0187",
            "jordan.lee@example.test")  # fmt: skip
QUOTE = "[TENANT_FIRST_NAME]: Hi, there is water leaking from the bathroom ceiling."
REAL_QUOTE = "Jordan: Hi, there is water leaking from the bathroom ceiling."


@pytest.fixture
def on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "privacy_placeholders", True)


def store() -> InMemoryTextStore:
    s = demo_store()
    s.add_pages(CASE, "ev_02", [Page("page 1", LETTER)])
    return s


def leak(source: dict[str, str], tool_id: str, title: str = "Tenant reports the leak") -> dict:
    return tool_use(
        "propose_event", tool_id, title=title,
        description="[TENANT_NAME] told [LANDLORD_FIRST_NAME] about the leak.",
        actor="tenant", sources=[source],
        date_candidates=[{"value": "2026-08-15", "precision": "day",
                          "basis": "message_timestamp"}],
    )  # fmt: skip


def script() -> dict[str, llm.LLMResponse]:
    return {
        "timeline-turn1": turn(
            thinking("read"), tool_use("read_evidence", "u1", evidence_id="ev_04", page=1),
            tool_use("read_evidence", "u2", evidence_id="ev_02"),
        ),
        "timeline-turn2": turn(
            leak({"evidence_id": "ev_04", "locator": "message 1", "excerpt": QUOTE}, "u3"),
            leak({"evidence_id": "ev_04", "locator": "message 1",
                  "excerpt": "[TENANT_FIRST_NAME]: The landlord admits it."}, "u4", "Invented"),
        ),
        "timeline-turn3": turn(tool_use("finish", "u5", summary="[TENANT_NAME]'s leak, 1 event.")),
    }


def execute(session: Session, provider: llm.LLMProvider) -> RunRow:
    case = session.get(CaseRow, CASE)
    assert case is not None
    row = runner.create_run(session, case)
    return run(runner.execute_run(session, row, provider, store(), nonce=N, steps=("timeline",)))


def strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    return []


def sent_text(provider: RecordingProvider) -> str:
    return "\n".join(
        s for r in provider.requests for m in r.messages for s in strings(m.model_dump()["content"])
    )


def new_events(session: Session) -> list[TimelineEventRow]:
    session.expire_all()
    rows = session.exec(select(TimelineEventRow).where(TimelineEventRow.case_id == CASE))
    return [e for e in rows if len(e.id) == 13]


# --- a run


def test_the_model_never_sees_the_cases_personal_details(
    fresh_db: None, session: Session, on: None
) -> None:
    provider = RecordingProvider(script())
    execute(session, provider)
    sent = sent_text(provider)
    assert "[TENANT_FIRST_NAME]: Hi, there is water leaking" in sent
    assert "[TENANT_NAME], [RENTAL_ADDRESS], Halifax, NS" in sent
    for value in PERSONAL:
        assert value not in sent, value
    assert all(placeholders.LEGEND in r.system_run for r in provider.requests)


def test_what_the_model_proposes_is_saved_with_the_real_values(
    fresh_db: None, session: Session, on: None
) -> None:
    row = execute(session, RecordingProvider(script()))
    step = next(s for s in row.steps if s["key"] == "timeline")
    assert step["summary"].startswith("Jordan Lee's leak, 1 event.")
    [event] = new_events(session)  # the invented quote is still rejected
    assert event.sources[0]["excerpt"] == REAL_QUOTE
    # A placeholder the model was never given stays as the model wrote it.
    assert event.description == "Jordan Lee told [LANDLORD_FIRST_NAME] about the leak."
    calls = [c for c in step["tool_calls"] if c["tool"] == "propose_event"]
    assert "Jordan: Hi" in calls[0]["input"] and "[TENANT_FIRST_NAME]" not in calls[0]["input"]
    assert "citation rejected" in calls[1]["output"]


def test_thinking_comes_back_unchanged_and_the_history_is_resent_with_placeholders(
    fresh_db: None, session: Session, on: None
) -> None:
    provider = RecordingProvider(script())
    execute(session, provider)
    second = provider.requests[1].messages
    assert second[1].content == script()["timeline-turn1"].content  # as the model wrote it
    results = json.dumps(second[2].model_dump()["content"], ensure_ascii=False)
    assert "[TENANT_FIRST_NAME]: Hi" in results and "Jordan" not in results


def test_with_the_setting_off_the_evidence_goes_as_written(
    fresh_db: None, session: Session
) -> None:
    provider = RecordingProvider(script())
    execute(session, provider)
    sent = sent_text(provider)
    # (The scripted replies themselves carry placeholders; the evidence sent does not.)
    assert MSG1 in sent and "[RENTAL_ADDRESS]" not in sent
    assert all(placeholders.LEGEND not in r.system_run for r in provider.requests)


def test_the_wrapper_keeps_the_model_name_and_paused_run_profile() -> None:
    inner = llm.FixtureProvider(model="claude-haiku-5-5")
    wrapped = placeholders.PlaceholderProvider(inner, placeholders.Placeholders())
    assert llm.model_name(wrapped) == "claude-haiku-5-5"
    assert llm.continuation_key(wrapped) == llm.continuation_key(inner)


def test_a_placeholder_written_against_a_letter_is_never_resent_as_the_real_value() -> None:
    """Issue #164: `reveal` turns `[TENANT_NAME]s` into `Jordan Lees`; `hide` would not find
    `Jordan Lee` before an `s`, and the surname would reach the provider on the next call."""
    from app.services.personal_details import Placeholders, name_variants

    ph = Placeholders(known=name_variants("Jordan Lee", "TENANT"))
    ph.hide("Jordan Lee")  # the case's own name gets [TENANT_NAME]
    wrapped = placeholders.PlaceholderProvider(llm.FixtureProvider(), ph)
    wrote = [
        {"type": "text", "text": "Note on the [TENANT_NAME]s unit and [TENANT_FIRST_NAME]2."},
        {"type": "tool_use", "id": "tu_1", "name": "x", "input": {"d": "the [TENANT_NAME]s"}},
    ]
    revealed = wrapped.reveal_response(llm.LLMResponse(content=wrote, text=wrote[0]["text"]))
    assert "Jordan Lees" in revealed.content[0]["text"]  # the app works on real text
    assert revealed.content[1]["input"] == {"d": "the Jordan Lees"}

    request = llm.LLMRequest(system="s", fixture_key="k", messages=[
        llm.Message(role="user", content="Jordan Lee wrote this."),
        llm.Message(role="assistant", content=revealed.content),
        llm.Message(role="user", content=[{
            "type": "tool_result", "tool_use_id": "tu_1", "content": "Jordan Lee, unit 4B",
        }]),
    ])
    sent = wrapped.hide_request(request).messages
    assert sent[1].content == wrote  # exactly what the model wrote
    flat = json.dumps([m.model_dump() for m in sent], ensure_ascii=False)
    assert "Jordan" not in flat and "Lee" not in flat
    assert sent[0].content == "[TENANT_NAME] wrote this."

    # An assistant block the model did not write this run (a resumed history) is still hidden.
    other = llm.Message(role="assistant", content=[{"type": "text", "text": "Hi Jordan Lee"}])
    resumed = llm.LLMRequest(system="s", fixture_key="k", messages=[other])
    assert wrapped.hide_request(resumed).messages[0].content == [
        {"type": "text", "text": "Hi [TENANT_NAME]"}
    ]


# --- a run that pauses and resumes (issue #163)


def pause_with_letter(session: Session) -> tuple[RunRow, RecordingProvider]:
    """A timeline run that reads the letter, asks the caseworker a question and pauses."""
    case = session.get(CaseRow, CASE)
    assert case is not None
    first = RecordingProvider({"timeline-turn1": turn(
        tool_use("read_evidence", "u1", evidence_id="ev_02"),
        tool_use("ask_caseworker", "u2", question="Is the hearing booked?"),
    )})
    row = runner.create_run(session, case)
    row = run(runner.execute_run(session, row, first, store(), nonce=N, steps=("timeline",)))
    assert row.steps[2]["status"] == "needs_input"
    return row, first


def resume_run(session: Session, row: RunRow) -> RecordingProvider:
    second = RecordingProvider({"timeline-turn2": turn(tool_use("finish", "u3", summary="Done."))})
    run(runner.execute_run(
        session, row, second, store(), nonce=N, steps=("timeline",), answer="Yes",
    ))
    return second


def test_a_resumed_run_keeps_the_placeholders_it_had_before_the_pause(
    fresh_db: None, session: Session, on: None
) -> None:
    """The saved conversation holds real text and is hidden again on every call. A value that
    became a known one during the pause (the draft step filled the landlord's address) must not
    get a new placeholder, or the model would see one value under two names."""
    row, _ = pause_with_letter(session)
    # The letter holds "22 Placeholder Road." and the pause saved it as [ADDRESS_1].
    assert any(e[1] == "[ADDRESS_1]" and e[0] == "22 Placeholder Road."
               for e in row.steps[0][runner.PLACEHOLDERS_KEY]["entries"])

    field = session.exec(select(FormFieldRow).where(
        FormFieldRow.case_id == CASE, FormFieldRow.key == "landlord_address")).one()
    field.value = "22 Placeholder Road."  # as the draft step copies it from the letter
    session.add(field)
    session.commit()
    session.refresh(row)

    after = sent_text(resume_run(session, row))
    assert "22 Placeholder Road" not in after
    assert "[ADDRESS_1]" in after and "[LANDLORD_ADDRESS]" not in after


def test_the_numbering_is_saved_with_the_run_and_stays_out_of_the_api(
    fresh_db: None, session: Session, on: None, client
) -> None:
    row, _ = pause_with_letter(session)
    saved = row.steps[0][runner.PLACEHOLDERS_KEY]
    assert any(e[:2] == ["22 Placeholder Road.", "[ADDRESS_1]"] for e in saved["entries"])
    assert row.steps[0][runner.PRIVACY_KEY] is True
    body = client.get(f"/api/runs/{row.id}").json()
    for step in body["steps"]:  # the map holds real values: the API never returns it
        assert set(step) == {"key", "label", "status", "summary", "tool_calls"}


def test_switching_the_setting_during_a_pause_refuses_the_resume(
    fresh_db: None, session: Session, on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, _ = pause_with_letter(session)
    provider = RecordingProvider({"timeline-turn2": turn(tool_use("finish", "u3", summary="x"))})
    assert runner.can_resume_with(row, provider)
    monkeypatch.setattr(get_settings(), "privacy_placeholders", False)
    assert not runner.can_resume_with(row, provider)
    with pytest.raises(llm.LLMConfigError):
        run(runner.execute_run(
            session, row, provider, store(), nonce=N, steps=("timeline",), answer="Yes"))
    assert provider.requests == []  # nothing was sent, and no placeholder text was saved


def test_a_run_paused_with_the_setting_off_refuses_when_it_is_turned_on(
    fresh_db: None, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, _ = pause_with_letter(session)
    provider = RecordingProvider({})
    assert runner.can_resume_with(row, provider)
    monkeypatch.setattr(get_settings(), "privacy_placeholders", True)
    assert not runner.can_resume_with(row, provider)


def test_a_run_saved_before_the_setting_was_recorded_still_resumes(
    fresh_db: None, session: Session, on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    row, _ = pause_with_letter(session)
    row.steps = [{k: v for k, v in s.items() if k not in (runner.PRIVACY_KEY,
                                                         runner.PLACEHOLDERS_KEY)}
                 for s in row.steps]
    assert runner.can_resume_with(row, RecordingProvider({}))
    monkeypatch.setattr(get_settings(), "privacy_placeholders", False)
    assert runner.can_resume_with(row, RecordingProvider({}))  # no flag: not checked


def test_the_saved_numbering_is_dropped_once_the_run_cannot_resume(
    fresh_db: None, session: Session, on: None
) -> None:
    row, _ = pause_with_letter(session)
    assert runner.PLACEHOLDERS_KEY in row.steps[0]  # paused: it can resume
    resume_run(session, row)
    session.refresh(row)
    assert runner.PLACEHOLDERS_KEY not in row.steps[0]  # finished: real values not kept


def test_a_glued_placeholder_from_before_the_pause_is_still_not_resent_real(
    fresh_db: None, session: Session, on: None
) -> None:
    """Issue #164 across a resume: what the model wrote is saved with the run."""
    case = session.get(CaseRow, CASE)
    assert case is not None
    first = RecordingProvider({"timeline-turn1": turn(
        {"type": "text", "text": "Note on the [TENANT_NAME]s unit."},
        tool_use("ask_caseworker", "u2", question="Is the hearing booked?"),
    )})
    row = runner.create_run(session, case)
    row = run(runner.execute_run(session, row, first, store(), nonce=N, steps=("timeline",)))
    second = resume_run(session, row)
    sent = sent_text(second)
    assert "Note on the [TENANT_NAME]s unit." in sent
    assert "Lees" not in sent and "Jordan" not in sent


# --- the caseworker's list


def privacy(client) -> dict:
    r = client.get(f"/api/cases/{CASE}/privacy")
    assert r.status_code == 200, r.text
    return r.json()


def test_the_list_shows_the_cases_details_and_where_they_came_from(
    fresh_db: None, client, on: None
) -> None:
    view = privacy(client)
    assert view["enabled"] is True
    by_value = {d["value"]: d for d in view["details"]}
    assert by_value["Jordan Lee"]["placeholder"] == "[TENANT_NAME]"
    assert by_value["Jordan Lee"]["source"] == "case"
    assert by_value["Unit 4B, 100 Example Street"]["placeholder"] == "[RENTAL_ADDRESS]"
    assert "Sample Property Management Ltd." not in by_value  # a company is kept


def test_a_hidden_name_is_listed_and_logged_without_its_value(
    fresh_db: None, client, session: Session, on: None
) -> None:
    r = client.post(f"/api/cases/{CASE}/privacy/terms",
                    json={"value": "约旦", "action": "hide", "role": "tenant"})  # fmt: skip
    assert r.status_code == 200, r.text
    added = next(d for d in r.json()["details"] if d["value"] == "约旦")
    assert (added["placeholder"], added["source"]) == ("[TENANT_NAME_2]", "caseworker")
    logged = session.exec(select(ActivityRow).where(ActivityRow.case_id == CASE)).all()
    assert any(a.action == "Hid a personal detail from the AI model" for a in logged)
    assert not any("约旦" in a.action for a in logged)


def test_a_value_let_through_is_sent_as_written_and_can_be_undone(
    fresh_db: None, client, on: None
) -> None:
    r = client.post(f"/api/cases/{CASE}/privacy/terms",
                    json={"value": "jordan lee", "action": "show"})  # fmt: skip
    shown = next(d for d in r.json()["details"] if d["shown"])
    assert (shown["value"], shown["placeholder"]) == ("Jordan Lee", "[TENANT_NAME]")
    r = client.delete(f"/api/cases/{CASE}/privacy/terms/{shown['term_id']}")
    assert r.status_code == 200
    assert not any(d["shown"] for d in r.json()["details"])


def test_a_bad_entry_is_refused_with_a_plain_reason(fresh_db: None, client) -> None:
    for body in ({"value": "x", "action": "hide"}, {"value": "Jordan", "action": "delete"},
                 {"value": "Jordan", "action": "hide", "role": "pet"}, None):
        r = client.post(f"/api/cases/{CASE}/privacy/terms", json=body)
        assert r.status_code == 422 and r.json()["detail"]["reasons"]


def test_another_orgs_case_and_terms_are_404(fresh_db: None, client, other_org_client) -> None:
    assert other_org_client.get(f"/api/cases/{CASE}/privacy").status_code == 404
    r = other_org_client.post(f"/api/cases/{CASE}/privacy/terms",
                              json={"value": "Jordan", "action": "hide"})  # fmt: skip
    assert r.status_code == 404
    r = client.post(f"/api/cases/{CASE}/privacy/terms", json={"value": "Lee", "action": "show"})
    term = next(d["term_id"] for d in r.json()["details"] if d["shown"])
    assert other_org_client.delete(f"/api/cases/{CASE}/privacy/terms/{term}").status_code == 404
    assert client.delete(f"/api/cases/{CASE}/privacy/terms/pt_missing").status_code == 404


def test_model_text_shows_placeholders_only_when_the_setting_is_on(
    fresh_db: None, client, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.models import EvidencePageRow

    session.add(EvidencePageRow(case_id=CASE, evidence_id="ev_04", position=0,
                                locator="message 1", text=MSG1))  # fmt: skip
    session.commit()
    path = f"/api/cases/{CASE}/evidence/ev_04/model-text"
    assert client.get(path).json()["pages"][0]["text"] == MSG1
    monkeypatch.setattr(get_settings(), "privacy_placeholders", True)
    body = client.get(path).json()
    assert body["label"] and body["pages"][0]["text"].startswith(
        "[2026-08-15 09:12] [TENANT_FIRST_NAME]: Hi"
    )
    assert client.get(f"/api/cases/{CASE}/evidence/ev_missing/model-text").status_code == 404


# --- #166: hiding a value the case already hides


def terms(session: Session) -> list[PrivacyTermRow]:
    session.expire_all()
    return list(session.exec(select(PrivacyTermRow).where(PrivacyTermRow.case_id == CASE)))


def rows_for(view: dict, value: str) -> list[dict]:
    return [d for d in view["details"] if d["value"] == value and not d["shown"]]


def placeholder_map(view: dict) -> dict[str, str]:
    return {d["value"]: d["placeholder"] for d in view["details"] if not d["shown"]}


@pytest.mark.parametrize(("value", "placeholder"), [
    ("Jordan Lee", "[TENANT_NAME]"),  # the tenant's name, exactly as the case has it
    ("Jordan", "[TENANT_FIRST_NAME]"),  # only inside the name in the seeded case: count 0
])  # fmt: skip
def test_hiding_a_value_the_case_already_hides_lists_the_term_until_it_is_removed(
    fresh_db: None, client, session: Session, on: None, value: str, placeholder: str
) -> None:
    before = placeholder_map(privacy(client))
    r = client.post(f"/api/cases/{CASE}/privacy/terms",
                    json={"value": value, "action": "hide", "role": "tenant"})  # fmt: skip
    assert r.status_code == 200, r.text
    [row] = terms(session)
    term_id = row.id
    for view in (r.json(), privacy(client)):  # the reply and the list agree with the table
        [added] = rows_for(view, value)
        assert (added["term_id"], added["source"]) == (term_id, "caseworker")
        assert added["placeholder"] == placeholder
        assert {v: p for v, p in placeholder_map(view).items() if v in before} == before
    r = client.delete(f"/api/cases/{CASE}/privacy/terms/{term_id}")
    assert r.status_code == 200, r.text
    assert terms(session) == []
    for view in (r.json(), privacy(client)):
        assert not any(d["term_id"] == term_id for d in view["details"])
        assert placeholder_map(view) == before
    [name] = rows_for(privacy(client), "Jordan Lee")  # still hidden by the case itself
    assert (name["placeholder"], name["source"], name["term_id"]) == ("[TENANT_NAME]", "case", None)


def test_a_duplicate_term_changes_nothing_the_model_receives(
    fresh_db: None, client, session: Session, on: None
) -> None:
    def sent() -> str:
        provider = RecordingProvider(script())
        execute(session, provider)
        return sent_text(provider)

    def hidden() -> str:
        case = session.get(CaseRow, CASE)
        assert case is not None
        session.expire_all()
        return placeholders.build(session, case, store()).hide(f"{LETTER}\n{MSG1}")

    def check(text: str) -> None:
        assert "[TENANT_FIRST_NAME]: Hi, there is water leaking" in text
        assert "[TENANT_NAME], [RENTAL_ADDRESS], Halifax, NS" in text
        for value in PERSONAL:
            assert value not in text, value

    baseline = hidden()
    for value in ("Jordan Lee", "Jordan"):
        r = client.post(f"/api/cases/{CASE}/privacy/terms",
                        json={"value": value, "action": "hide", "role": "person"})  # fmt: skip
        assert r.status_code == 200, r.text
    assert len(terms(session)) == 2
    assert hidden() == baseline
    check(sent())  # with the terms
    for row in terms(session):
        assert client.delete(f"/api/cases/{CASE}/privacy/terms/{row.id}").status_code == 200
    assert terms(session) == []
    assert hidden() == baseline
    check(sent())  # after Remove, the model still gets the case's own placeholders


def test_hiding_the_same_value_twice_keeps_one_term(
    fresh_db: None, client, session: Session, on: None
) -> None:
    for value in ("Jordan", "JORDAN"):
        r = client.post(f"/api/cases/{CASE}/privacy/terms",
                        json={"value": value, "action": "hide", "role": "tenant"})  # fmt: skip
        assert r.status_code == 200, r.text
    [row] = terms(session)
    listed = [d for d in privacy(client)["details"] if d["term_id"] == row.id]
    assert [(d["value"], d["placeholder"]) for d in listed] == [("Jordan", "[TENANT_FIRST_NAME]")]


def test_a_new_value_is_listed_as_before(
    fresh_db: None, client, session: Session, on: None
) -> None:
    before = placeholder_map(privacy(client))
    body = {"value": "Example Street", "action": "hide", "role": "other"}
    r = client.post(f"/api/cases/{CASE}/privacy/terms", json=body)
    [row] = terms(session)
    [added] = rows_for(r.json(), "Example Street")  # only inside the rental address: count 0
    assert (added["placeholder"], added["source"], added["count"], added["term_id"]) == (
        "[DETAIL_1]", "caseworker", 0, row.id)
    assert {v: p for v, p in placeholder_map(r.json()).items() if v in before} == before
