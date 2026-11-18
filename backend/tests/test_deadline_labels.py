"""The deadline label is in one wording wherever it came from, and a seeded demo case keeps its
deadline relative to today. Pure: no database for the first half."""

from datetime import date, timedelta

import pytest

from app.models import CaseRow
from app.services import dates
from app.services.dates import canonical_deadline_label, demo_deadline
from app.services.mappers import to_summary

TODAY = date(2026, 11, 20)


@pytest.mark.parametrize("stored", [
    "Evidence due · hearing 2026-11-21",
    "evidence due · hearing 2026-11-21",
    "  Evidence Due · hearing 2026-11-21",
    "Must be received by · hearing 2026-11-21",
])
def test_every_stored_wording_reads_the_same(stored: str):
    assert canonical_deadline_label(stored) == "Must be received by · hearing 2026-11-21"


def test_only_the_leading_wording_is_changed():
    label = "Must be received by · hearing 2026-11-21 (evidence due soon)"
    assert canonical_deadline_label(label) == label


def test_the_label_the_rules_step_writes_is_the_canonical_one():
    made = dates.evidence_deadline(date(2026, 11, 21))
    assert made["label"] == "Must be received by · hearing 2026-11-21"
    assert canonical_deadline_label(made["label"]) == made["label"]


def test_a_deadline_without_an_offset_is_returned_as_stored_in_todays_wording():
    stored = {"date": "2026-11-16", "label": "Evidence due · hearing 2026-11-21"}
    assert demo_deadline(stored, TODAY) == {
        "date": "2026-11-16", "label": "Must be received by · hearing 2026-11-21"}


@pytest.mark.parametrize("offset", [2, 10])
@pytest.mark.parametrize("days_later", [0, 1, 9, 400])
def test_a_demo_deadline_stays_the_same_distance_ahead_of_today(offset: int, days_later: int):
    stored = {"date": "2026-11-08", "label": "Must be received by · hearing 2026-11-13",
              "demo_offset_days": offset}
    today = TODAY + timedelta(days=days_later)
    moved = demo_deadline(stored, today)
    due = date.fromisoformat(moved["date"])
    assert (due - today).days == offset  # never in the past
    hearing = (due + timedelta(days=5)).isoformat()
    assert moved["label"] == f"Must be received by · hearing {hearing}"


def test_the_demo_deadlines_keep_their_order():
    a = demo_deadline(
        {"date": "2026-11-08", "label": "x · hearing 2026-11-13", "demo_offset_days": 2}, TODAY)
    b = demo_deadline(
        {"date": "2026-11-16", "label": "x · hearing 2026-11-21", "demo_offset_days": 10}, TODAY)
    assert a["date"] < b["date"]


def _case(deadline: dict | None) -> CaseRow:
    return CaseRow(
        id="c", org_id="o", assigned_to_id="u", title="t", unit="u", tenant_name="n",
        tenant_language="en", stage="hearing_scheduled", next_deadline=deadline,
        form_name="", form_disclaimer="",
    )


def test_the_caseload_row_of_an_old_seed_reads_ahead_and_in_the_new_wording():
    stored = {"date": "2026-11-08", "label": "Evidence due · hearing 2026-11-13",
              "demo_offset_days": 2}
    summary, _ = to_summary(_case(stored), 0, 0, None, date(2027, 3, 1))
    assert summary.next_deadline is not None
    assert summary.next_deadline.date == "2027-03-03"
    assert summary.next_deadline.label == "Must be received by · hearing 2027-03-08"
    assert summary.priority_reason == "Must be received in 2 days (5 days before hearing)"


def test_a_real_deadline_that_has_passed_says_so_in_the_row():
    stored = {"date": "2026-11-18", "label": "Evidence due · hearing 2026-11-23"}
    summary, _ = to_summary(_case(stored), 0, 0, None, TODAY)
    assert summary.priority_reason == "Deadline passed (2 days ago)"
    assert summary.next_deadline is not None
    assert summary.next_deadline.label.startswith("Must be received by")
