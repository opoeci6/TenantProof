from datetime import date, timedelta

import pytest

from app.services.priority import PriorityInput, compute_priority, deadline_phrase, sort_key

TODAY = date(2026, 11, 6)


def case(**kw):
    base = {"stage": "needs_review", "next_deadline_date": None, "open_blocking": 0,
            "pending_review": 0}
    return PriorityInput(**{**base, **kw})


def test_deadline_within_7_days_is_high():
    r = compute_priority(case(next_deadline_date=date(2026, 11, 8)), TODAY)
    assert r.priority == "high"
    assert r.reason == "Must be received in 2 days (5 days before hearing)"


def test_deadline_exactly_7_days_is_high_and_8_is_normal():
    assert compute_priority(case(next_deadline_date=date(2026, 11, 13)), TODAY).priority == "high"
    assert compute_priority(case(next_deadline_date=date(2026, 11, 14)), TODAY).priority == "normal"


def test_one_day_is_singular():
    r = compute_priority(case(next_deadline_date=date(2026, 11, 7)), TODAY)
    assert "1 day " in r.reason


def test_blocking_gap_with_hearing_is_high_and_reason_combines():
    r = compute_priority(
        case(stage="hearing_scheduled", open_blocking=1, next_deadline_date=date(2026, 11, 8)),
        TODAY,
    )
    assert r.priority == "high"
    assert "and 1 required item open with a hearing scheduled" in r.reason


def test_blocking_gap_without_hearing_is_normal():
    r = compute_priority(case(open_blocking=2, pending_review=7), TODAY)
    assert r.priority == "normal"
    assert r.reason == "7 events to review · 2 required items open"


def test_waiting_tenant_and_empty_reasons():
    waiting = compute_priority(case(stage="waiting_tenant"), TODAY)
    assert "No files from the tenant" in waiting.reason
    assert compute_priority(case(stage="ready_to_file"), TODAY).reason == "No open items"


def test_a_link_never_sent_is_not_called_sent():
    """A case just created (#41) waits for the tenant before any link went out."""
    unsent = compute_priority(case(stage="waiting_tenant", intake_sent=False), TODAY)
    assert unsent.reason == "Intake link not sent yet."
    # a deadline still wins, as for any waiting case
    soon = case(stage="waiting_tenant", intake_sent=False, next_deadline_date=date(2026, 11, 8))
    assert compute_priority(soon, TODAY).reason.startswith("Must be received in 2 days")


def test_overdue_deadline_is_high():
    r = compute_priority(case(next_deadline_date=date(2026, 11, 1)), TODAY)
    assert r.priority == "high"
    assert r.days_to_deadline == -5


def test_sort_order_high_then_deadline_then_blocking():
    keys = [
        ("normal-far", sort_key("normal", 10, 0, 2)),
        ("high-late", sort_key("high", 5, 0, 0)),
        ("high-soon", sort_key("high", 2, 1, 0)),
        ("normal-none-blocking", sort_key("normal", None, 3, 0)),
        ("normal-none", sort_key("normal", None, 0, 0)),
    ]
    assert [n for n, _ in sorted(keys, key=lambda x: x[1])] == [
        "high-soon", "high-late", "normal-far", "normal-none-blocking", "normal-none"]


# --- how the deadline reads (0, 1, many and past days; singular and plural counts) --------------


@pytest.mark.parametrize(("days", "text"), [
    (7, "Must be received in 7 days"),
    (2, "Must be received in 2 days"),
    (1, "Must be received in 1 day"),
    (0, "Must be received today"),
    (-1, "Deadline passed (1 day ago)"),
    (-2, "Deadline passed (2 days ago)"),
    (-30, "Deadline passed (30 days ago)"),
])
def test_deadline_phrase(days: int, text: str):
    assert deadline_phrase(days) == text


@pytest.mark.parametrize("days", [-1, -5])
def test_a_passed_deadline_never_shows_a_negative_count(days: int):
    r = compute_priority(case(next_deadline_date=TODAY + timedelta(days=days)), TODAY)
    assert r.priority == "high"
    assert r.reason == deadline_phrase(days)
    assert "-" not in r.reason and "before hearing" not in r.reason


def test_a_deadline_today_and_tomorrow_read_naturally():
    today = compute_priority(case(next_deadline_date=TODAY), TODAY)
    tomorrow = compute_priority(case(next_deadline_date=TODAY + timedelta(days=1)), TODAY)
    assert today.reason == "Must be received today (5 days before hearing)"
    assert tomorrow.reason == "Must be received in 1 day (5 days before hearing)"


def test_a_far_deadline_has_no_priority_but_still_reads():
    r = compute_priority(case(next_deadline_date=TODAY + timedelta(days=9)), TODAY)
    assert r.priority == "normal" and r.reason == "Must be received in 9 days"


@pytest.mark.parametrize(("blocking", "text"), [
    (1, "1 required item open with a hearing scheduled"),
    (2, "2 required items open with a hearing scheduled"),
    (7, "7 required items open with a hearing scheduled"),
])
def test_required_items_are_pluralised(blocking: int, text: str):
    r = compute_priority(case(stage="hearing_scheduled", open_blocking=blocking), TODAY)
    assert r.reason == text


@pytest.mark.parametrize(("pending", "blocking", "text"), [
    (1, 1, "1 event to review · 1 required item open"),
    (2, 1, "2 events to review · 1 required item open"),
    (1, 3, "1 event to review · 3 required items open"),
    (0, 0, "0 events to review · 0 required items open"),
])
def test_the_review_counts_are_pluralised(pending: int, blocking: int, text: str):
    r = compute_priority(case(pending_review=pending, open_blocking=blocking), TODAY)
    if pending == 0 and blocking == 0:
        assert r.reason == "No open items"
    else:
        assert r.reason == text
