"""The timeline's "Accept all" (#87). `acceptAllPlan` and `acceptAllToast` are pure functions in
web/index.js, run under node. An event the agent flagged as a conflict keeps the strength the
rules gave it (often single-source), so Accept all has to look at the flags too."""

import json
import shutil

import pytest

from tests.test_empty_states import INDEX, js

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

CASE = "case_demo_001"
CONFLICT = {"type": "conflict", "message": "Sources disagree: the photo's capture time is earlier."}


def event(eid: str, strength: str, *, status: str = "pending", flags: list | None = None) -> dict:
    return {"id": eid, "strength": strength, "review_status": status, "flags": flags or []}


def plan(timeline: list[dict]) -> dict:
    return js(f"acceptAllPlan({json.dumps(timeline)})")  # type: ignore[return-value]


def test_a_conflict_event_is_left_even_when_single_source() -> None:
    timeline = [
        event("te_1", "corroborated"),
        event("te_2", "single_source", flags=[CONFLICT]),
        event("te_3", "single_source"),
        event("te_4", "needs_support"),
        event("te_5", "corroborated", flags=[CONFLICT]),
    ]
    assert plan(timeline) == {"accept": ["te_1", "te_3"], "conflict": 2, "needsSupport": 1}


def test_other_flags_do_not_hold_an_event_back() -> None:
    flags = [{"type": "no_metadata", "message": "A photo has no capture time."}]
    assert plan([event("te_1", "single_source", flags=flags)])["accept"] == ["te_1"]


def test_reviewed_events_are_not_touched_or_counted() -> None:
    timeline = [
        event("te_1", "single_source", status="accepted", flags=[CONFLICT]),
        event("te_2", "needs_support", status="rejected"),
        event("te_3", "corroborated", status="edited"),
    ]
    assert plan(timeline) == {"accept": [], "conflict": 0, "needsSupport": 0}


def test_an_event_with_a_conflict_and_needing_support_counts_once() -> None:
    timeline = [event("te_1", "needs_support", flags=[CONFLICT])]
    assert plan(timeline) == {"accept": [], "conflict": 1, "needsSupport": 0}


@pytest.mark.parametrize("p, text", [
    ({"accept": ["a", "b"], "conflict": 0, "needsSupport": 0}, "Accepted 2 events."),
    ({"accept": ["a"], "conflict": 1, "needsSupport": 0},
     "Accepted 1 event. Left for you to review: 1 with a conflict."),
    ({"accept": ["a", "b", "c"], "conflict": 1, "needsSupport": 2},
     "Accepted 3 events. Left for you to review: 1 with a conflict and 2 that need support."),
    ({"accept": [], "conflict": 0, "needsSupport": 1},
     "Nothing to accept. Left for you to review: 1 that needs support."),
    ({"accept": [], "conflict": 0, "needsSupport": 0}, "Nothing to accept."),
])  # fmt: skip
def test_the_toast_says_what_was_left(p: dict, text: str) -> None:
    assert js(f"acceptAllToast({json.dumps(p)})") == text


def test_the_button_uses_the_plan() -> None:
    html = INDEX.read_text(encoding="utf-8")
    handler = html[html.index('if (a === "accept-all")'):]
    handler = handler[: handler.index("\n  }")]
    assert "acceptAllPlan(S.timeline)" in handler
    assert "acceptAllToast(plan)" in handler
    assert "needs_support" not in handler  # the rule lives in acceptAllPlan only


def test_a_flagged_event_from_the_api_is_left(fresh_db: None, client, session) -> None:
    """The issue's case: a single-source event with a conflict flag, as the timeline API returns
    it after the agent's `flag_conflict`."""
    from app.models import TimelineEventRow

    events = client.get(f"/api/cases/{CASE}/timeline").json()
    target = next(
        e for e in events if e["review_status"] == "pending" and e["strength"] == "single_source"
    )
    row = session.get(TimelineEventRow, target["id"])
    row.flags = [*row.flags, CONFLICT]
    session.add(row)
    session.commit()

    events = client.get(f"/api/cases/{CASE}/timeline").json()
    flagged = next(e for e in events if e["id"] == target["id"])
    assert flagged["strength"] == "single_source"
    result = plan(events)
    assert target["id"] not in result["accept"]
    assert result["conflict"] == 1
    for eid in result["accept"]:
        r = client.patch(f"/api/timeline/{eid}", json={"review_status": "accepted"})
        assert r.status_code == 200
    after = {e["id"]: e["review_status"] for e in client.get(f"/api/cases/{CASE}/timeline").json()}
    assert after[target["id"]] == "pending"
