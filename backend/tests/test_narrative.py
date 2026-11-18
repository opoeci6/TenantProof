from app.services.narrative import build_narrative, format_date


def ev(id_, status, value="2026-08-15", precision="day", desc="Something happened.", src="ev_1"):
    return {"id": id_, "review_status": status, "description": desc,
            "date": {"value": value, "precision": precision, "basis": "exif"},
            "sources": [{"evidence_id": src, "locator": "x", "excerpt": None}]}


LABELS = {"ev_1": "E-01", "ev_2": "E-02"}


def test_only_accepted_and_edited_events_are_used():
    events = [ev("a", "accepted"), ev("b", "rejected"), ev("c", "pending"), ev("d", "edited")]
    assert build_narrative(events, LABELS)["event_ids"] == ["a", "d"]


def test_rejected_text_never_appears():
    out = build_narrative([ev("b", "rejected", desc="SECRET REJECTED TEXT")], LABELS)
    assert out == {"text": "", "event_ids": []}


def test_no_events_gives_empty_narrative():
    assert build_narrative([], LABELS) == {"text": "", "event_ids": []}


def test_month_precision_and_unknown_dates():
    assert format_date({"value": "2026-09", "precision": "month"}) == "Sep 2026"
    assert format_date({"value": "2026-09", "precision": "month"}, True) == "Sep 2026 (month only)"
    assert format_date({"value": None, "precision": "unknown"}) == "Date unknown"
    assert format_date(None) == "Date unknown"


def test_labels_deduplicated_and_unknown_falls_back_to_id():
    e = ev("a", "accepted")
    e["sources"] = [{"evidence_id": "ev_1", "locator": "p1", "excerpt": None},
                    {"evidence_id": "ev_1", "locator": "p2", "excerpt": None},
                    {"evidence_id": "ev_9", "locator": "p1", "excerpt": None}]
    text = build_narrative([e], LABELS)["text"]
    assert text.endswith("[E-01, ev_9]")


def test_caseworker_note_never_in_narrative():
    e = ev("a", "accepted")
    e["caseworker_note"] = "INTERNAL NOTE"
    assert "INTERNAL NOTE" not in build_narrative([e], LABELS)["text"]


def test_event_without_sources_is_left_out_even_if_accepted():
    e = ev("a", "accepted", desc="UNSOURCED CLAIM")
    e["sources"] = []
    out = build_narrative([e, ev("b", "accepted")], LABELS)
    assert out["event_ids"] == ["b"]
    assert "UNSOURCED CLAIM" not in out["text"]
