"""One contract test per GET endpoint: the response must parse as the schema and expose
exactly the contract's field names (no extras, none missing)."""

import pytest

from app import schemas

CASE = "case_demo_001"


def keys(model) -> set[str]:
    return set(model.model_fields)


def test_me(client):
    r = client.get("/api/me")
    assert r.status_code == 200
    assert set(r.json()) == keys(schemas.User)
    assert r.json()["role"] == "caseworker"


def test_cases_sorted_by_priority_with_reasons(client):
    r = client.get("/api/cases")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 6  # five demo cases and the empty live-demo case (case_live_001)
    assert all(set(row) == keys(schemas.CaseSummary) for row in rows)
    assert rows[0]["tenant_name"] == "Priya Nair" and rows[0]["priority"] == "high"
    assert all(row["priority_reason"] for row in rows)


def test_case(client):
    r = client.get(f"/api/cases/{CASE}")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == keys(schemas.Case)
    assert set(body["intake"]) == keys(schemas.Intake)
    assert body["intake"]["url"] is None  # the raw link is only in the POST response


@pytest.mark.parametrize(
    ("path", "model", "count"),
    [("evidence", schemas.EvidenceItem, 7), ("timeline", schemas.TimelineEvent, 9),
     ("gaps", schemas.Gap, 5), ("sources", schemas.LegalSource, 4),
     ("activity", schemas.ActivityEntry, 5)],
)
def test_collections(client, path, model, count):
    r = client.get(f"/api/cases/{CASE}/{path}")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == count
    assert all(set(row) == keys(model) for row in rows)


def test_timeline_sorted_by_date(client):
    dates = [e["date"]["value"] for e in client.get(f"/api/cases/{CASE}/timeline").json()]
    assert dates == sorted(dates)


def test_activity_newest_first(client):
    ats = [a["at"] for a in client.get(f"/api/cases/{CASE}/activity").json()]
    assert ats == sorted(ats, reverse=True)


def test_run_latest_and_by_id(client):
    latest = client.get(f"/api/cases/{CASE}/runs/latest")
    assert latest.status_code == 200
    assert set(latest.json()) == keys(schemas.Run)
    assert len(latest.json()["steps"]) == 6
    assert client.get(f"/api/runs/{latest.json()['id']}").json() == latest.json()


def test_run_latest_404_when_none(client):
    assert client.get("/api/cases/case_003/runs/latest").status_code == 404


def test_form_draft_uses_only_accepted_events(client):
    body = client.get(f"/api/cases/{CASE}/form-draft").json()
    assert set(body) == keys(schemas.FormDraft)
    assert body["narrative"]["event_ids"] == ["te_01", "te_03"]
    assert len(body["fields"]) == 7


def test_intake_link_post_returns_url_once(client):
    r = client.post(f"/api/cases/{CASE}/intake-link")
    assert r.status_code == 200
    assert set(r.json()) == keys(schemas.Intake)
    assert r.json()["url"].startswith("http") and "/i/" in r.json()["url"]
    assert client.get(f"/api/cases/{CASE}").json()["intake"]["url"] is None


def test_intake_view_tenant_only_fields(client, token):
    r = client.get(f"/api/intake/{token}")
    assert r.status_code == 200
    assert set(r.json()) == keys(schemas.IntakeView)
    assert r.json()["language"] == "zh"


def test_ui_served_at_root_and_tenant_path(client):
    assert client.get("/").status_code == 200
    assert client.get("/i/anything").status_code == 200
    assert client.get("/healthz").json()["llm_mode"] == "fixture"
