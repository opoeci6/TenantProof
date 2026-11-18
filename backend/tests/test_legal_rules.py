"""The rules step: the case's sources and its evidence deadline. No model, no network."""

from dataclasses import replace
from datetime import UTC, datetime

from sqlmodel import select

from app.models import ActivityRow, CaseRow, LegalSourceRow
from app.services.agent import llm, runner
from app.services.legal import corpus, rules
from tests.agent_support import demo_store
from tests.agent_support import run as run_async

DEMO = "case_demo_001"  # seeded with four sources
LIVE = "case_live_001"  # seeded empty


def rows(session, case_id: str) -> list[LegalSourceRow]:
    return list(
        session.exec(select(LegalSourceRow).where(LegalSourceRow.case_id == case_id)).all()
    )


def test_the_seeded_rows_are_kept_and_only_the_missing_entry_is_added(fresh_db, session):
    case = session.get(CaseRow, DEMO)
    added, total, _ = rules.ensure_case_sources(session, case)
    session.commit()

    ids = {r.id for r in rows(session, DEMO)}
    assert {"src_01", "src_02", "src_03", "src_04"} <= ids  # the ids citations point at
    # the accepted-formats entry and the Regulations entry, which the seed does not carry
    assert added == 2
    # the four corpus entries plus the seed's Policy #28 row, which the corpus leaves out
    assert total == len(corpus.CORPUS) + 1


def test_a_second_run_adds_nothing(fresh_db, session):
    case = session.get(CaseRow, DEMO)
    rules.ensure_case_sources(session, case)
    added, total, _ = rules.ensure_case_sources(session, case)
    session.commit()
    assert added == 0
    assert total == len(corpus.CORPUS) + 1
    assert len(rows(session, DEMO)) == len(corpus.CORPUS) + 1


def test_an_empty_case_gets_the_whole_corpus(fresh_db, session):
    case = session.get(CaseRow, LIVE)
    added, total, _ = rules.ensure_case_sources(session, case)
    session.commit()
    assert added == len(corpus.CORPUS)
    assert total == len(corpus.CORPUS)


def test_new_rows_use_a_readable_id_built_from_case_and_entry(fresh_db, session):
    case = session.get(CaseRow, LIVE)
    rules.ensure_case_sources(session, case)
    session.commit()
    ids = {r.id for r in rows(session, LIVE)}
    assert f"src_{LIVE}_{corpus.CORPUS[0].id}" in ids


def test_the_seeded_evidence_policy_row_starts_unverified(fresh_db, session):
    """Nobody has confirmed the entry, so the seed must not say it was checked."""
    seeded = session.get(LegalSourceRow, "src_02")
    assert seeded is not None
    assert seeded.verify_status == "unverified" and seeded.last_verified_at is None


def _seeded_unchanged(session) -> LegalSourceRow:
    """The demo case's Evidence Policy row as if a source check had run on it."""
    checked = session.get(LegalSourceRow, "src_02")
    assert checked is not None and checked.url == corpus.EVIDENCE_POLICY_URL
    checked.verify_status = "unchanged"
    checked.last_verified_at = datetime(2026, 10, 31, 16, 20, tzinfo=UTC)
    session.add(checked)
    session.commit()
    return checked


def test_an_unconfirmed_entry_does_not_copy_a_seeded_status(fresh_db, session, monkeypatch):
    """A demo case row says unchanged, but nobody has confirmed the entry: a new case must not
    show it as checked. The corpus is made unconfirmed here, so the test does not depend on which
    entries a person has signed off."""
    unconfirmed = tuple(replace(e, confirmed_by=None, confirmed_on=None) for e in corpus.CORPUS)
    monkeypatch.setattr(rules, "CORPUS", unconfirmed)
    _seeded_unchanged(session)
    live = session.get(CaseRow, LIVE)
    rules.ensure_case_sources(session, live)
    session.commit()

    new = next(r for r in rows(session, LIVE) if r.url == corpus.EVIDENCE_POLICY_URL)
    assert new.verify_status == "unverified"
    assert new.last_verified_at is None


def test_a_confirmed_entry_copies_the_most_recently_checked_status(fresh_db, session, monkeypatch):
    checked = _seeded_unchanged(session)
    confirmed = tuple(
        replace(e, confirmed_by="A. Reviewer", confirmed_on="2026-11-16") for e in corpus.CORPUS
    )
    monkeypatch.setattr(rules, "CORPUS", confirmed)

    live = session.get(CaseRow, LIVE)
    rules.ensure_case_sources(session, live)
    session.commit()

    new = next(
        r for r in rows(session, LIVE)
        if r.url == corpus.EVIDENCE_POLICY_URL and r.title == checked.title
    )  # fmt: skip
    assert new.verify_status == "unchanged"
    assert new.last_verified_at == checked.last_verified_at


def test_an_entry_nobody_checked_stays_unverified_on_a_new_row(fresh_db, session):
    case = session.get(CaseRow, LIVE)
    rules.ensure_case_sources(session, case)
    session.commit()
    form = next(r for r in rows(session, LIVE) if r.kind == "form")
    assert form.verify_status == "unverified"
    assert form.last_verified_at is None


def test_the_titles_and_used_for_are_refreshed_from_the_corpus(fresh_db, session):
    case = session.get(CaseRow, DEMO)
    stale = next(r for r in rows(session, DEMO) if r.id == "src_01")
    stale.title = "an old title"
    session.add(stale)
    session.commit()

    rules.ensure_case_sources(session, case)
    session.commit()

    refreshed = session.get(LegalSourceRow, "src_01")
    assert refreshed is not None
    assert refreshed.title == corpus.CORPUS[0].title
    assert refreshed.id == "src_01"


OLD_FORM_J_URL = "https://beta.novascotia.ca/sites/default/files/documents/2-419/form-j.pdf"


def test_a_row_on_an_old_address_moves_to_the_corpus_address(fresh_db, session):
    """A case made before the corpus moved Form J kept the old URL, which the source check never
    reached: the row stayed "Not verified yet" for a confirmed source."""
    case = session.get(CaseRow, DEMO)
    form_j = session.get(LegalSourceRow, "src_03")
    assert form_j is not None
    form_j.url = OLD_FORM_J_URL
    session.add(form_j)
    session.commit()

    added, _, _ = rules.ensure_case_sources(session, case)
    session.commit()
    session.expire_all()

    entry = corpus.by_id("ns-form-j")
    assert entry is not None
    moved = session.get(LegalSourceRow, "src_03")
    assert moved is not None
    assert moved.url == entry.url
    assert not any(r.title == entry.title and r.id != "src_03" for r in rows(session, DEMO))
    assert added == 2  # the same two a fresh seed gets: no second Form J row


def test_the_deadline_comes_from_the_hearing_date(fresh_db, session):
    case = session.get(CaseRow, LIVE)
    case.hearing_date = "2026-11-21"
    session.add(case)
    session.commit()

    due = rules.apply_deadline(session, case)
    session.commit()

    assert due == "2026-11-16"  # five days before the hearing, from the corpus rule
    assert case.next_deadline == {
        "date": "2026-11-16", "label": "Must be received by · hearing 2026-11-21",
    }  # fmt: skip


def test_setting_the_deadline_is_logged(fresh_db, session):
    case = session.get(CaseRow, LIVE)
    case.hearing_date = "2026-11-21"
    session.add(case)
    session.commit()

    rules.apply_deadline(session, case)
    session.commit()

    lines = [
        a.action for a in session.exec(select(ActivityRow).where(ActivityRow.case_id == LIVE)).all()
        if a.actor_name == rules.ACTOR_NAME
    ]
    assert any("must-be-received-by date to 2026-11-16" in line for line in lines)


def test_the_same_deadline_twice_is_not_logged_twice(fresh_db, session):
    case = session.get(CaseRow, LIVE)
    case.hearing_date = "2026-11-21"
    session.add(case)
    session.commit()

    rules.apply_deadline(session, case)
    session.commit()
    rules.apply_deadline(session, case)
    session.commit()

    lines = [
        a for a in session.exec(select(ActivityRow).where(ActivityRow.case_id == LIVE)).all()
        if "must-be-received-by" in a.action
    ]
    assert len(lines) == 1


def test_no_hearing_date_means_no_deadline(fresh_db, session):
    case = session.get(CaseRow, LIVE)
    assert case.hearing_date is None
    assert rules.apply_deadline(session, case) is None
    assert case.next_deadline is None


def test_the_summary_says_there_is_no_hearing_date_yet(fresh_db, session):
    case = session.get(CaseRow, LIVE)
    result = rules.run(session, case)
    session.commit()

    assert result.deadline is None
    # nobody has confirmed an entry, so the demo case's seeded "unchanged" is not passed on
    assert result.summary == f"{len(corpus.CORPUS)} sources (0 checked), no hearing date yet"


def test_the_summary_names_the_deadline_when_there_is_one(fresh_db, session):
    case = session.get(CaseRow, LIVE)
    case.hearing_date = "2026-11-21"
    session.add(case)
    session.commit()

    result = rules.run(session, case)
    session.commit()

    assert result.summary.endswith("must be received by 2026-11-16")


def test_another_case_is_never_touched(fresh_db, session):
    before = {r.id for r in rows(session, DEMO)}
    live = session.get(CaseRow, LIVE)
    rules.run(session, live)
    session.commit()
    assert {r.id for r in rows(session, DEMO)} == before


def test_adding_sources_is_logged_once(fresh_db, session):
    case = session.get(CaseRow, LIVE)
    rules.ensure_case_sources(session, case)
    session.commit()

    lines = [
        a.action for a in session.exec(select(ActivityRow).where(ActivityRow.case_id == LIVE)).all()
        if a.actor_name == rules.ACTOR_NAME
    ]
    assert lines == [f"Added {len(corpus.CORPUS)} official sources to the case"]


# --- the runner hook -----------------------------------------------------------------------


def test_the_rules_step_runs_in_a_run_and_records_one_tool_call(fresh_db, session):
    """The step is deterministic, so the run needs a provider it never calls."""
    case = session.get(CaseRow, LIVE)
    case.hearing_date = "2026-11-21"
    session.add(case)
    session.commit()
    row = runner.create_run(session, case)

    done = run_async(runner.execute_run(
        session, row, llm.FixtureProvider(scripted={}), demo_store(), steps=("rules",)
    ))  # fmt: skip

    step = next(s for s in done.steps if s["key"] == "rules")
    assert step["status"] == "done"
    expected = f"{len(corpus.CORPUS)} sources (0 checked), must be received by 2026-11-16"
    assert step["summary"] == expected
    assert [call["tool"] for call in step["tool_calls"]] == ["rules"]
    assert "2026-11-16" in step["tool_calls"][0]["output"]
