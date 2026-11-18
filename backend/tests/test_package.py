"""Filing package: gating (422 before 409), build, download, versions, storage, activity,
caseworker_note leakage and org isolation. Runs against Postgres via the conftest fixtures."""

import hashlib
import io
import json
import zipfile
from datetime import datetime

import pytest
from pypdf import PdfReader
from sqlalchemy import LargeBinary
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from app import schemas
from app.models import ActivityRow, CaseRow, EvidenceRow, PackageRow, TimelineEventRow
from app.services import storage

CASE = "case_demo_001"
BUILD = f"/api/cases/{CASE}/package"
DOWNLOAD = f"/api/cases/{CASE}/package/download"
ZIP_NAMES = {"application_draft.pdf", "evidence_manifest.pdf", "manifest.json"}
PDF_NAMES = {"application_draft.pdf", "evidence_manifest.pdf"}
ALL_TRUE = {
    "reviewed_fields": True,
    "remedy_confirmed_with_tenant": True,
    "explained_not_legal_advice": True,
}
ATTESTED = {"attestations": ALL_TRUE}
REASON_LANDLORD = "Required item still open: Landlord's address for service"
REASON_REMEDY = "Required item still open: Confirm what the tenant is asking for"
REASON_TENANT = "The tenant hasn't confirmed their files are genuine"


# --- helpers -----------------------------------------------------------------------------


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_ready(client) -> None:
    """Resolve both blocking gaps so the only remaining gate is attestation/tenant state."""
    r1 = client.patch(
        "/api/gaps/gap_01", json={"status": "resolved", "value": "12 Harbour Rd, Halifax"}
    )
    r2 = client.patch(
        "/api/gaps/gap_02", json={"status": "resolved", "value": "Repair the ceiling leak"}
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text


def build(client, body: dict | None = ATTESTED):
    r = client.post(BUILD, json=body)
    assert r.status_code == 200, r.text
    return r


def download(client, version: int | None = None):
    params = {} if version is None else {"version": version}
    return client.get(DOWNLOAD, params=params)


def unzip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def package_rows(session) -> list[PackageRow]:
    session.expire_all()
    return list(session.exec(
        select(PackageRow).where(PackageRow.case_id == CASE).order_by(col(PackageRow.version))
    ).all())


def activity_rows(session) -> list[ActivityRow]:
    session.expire_all()
    return list(session.exec(
        select(ActivityRow).where(ActivityRow.case_id == CASE).order_by(col(ActivityRow.id))
    ).all())


def built_entries(session) -> list[ActivityRow]:
    return [a for a in activity_rows(session) if a.action.startswith("Built filing package")]


def reasons_of(r) -> list[str]:
    body = r.json()
    assert set(body) == {"detail"}, body
    detail = body["detail"]
    assert isinstance(detail, dict) and set(detail) == {"reasons"}, detail
    return detail["reasons"]


# --- gating: 422 (attestations) before 409 (state) ----------------------------------------


@pytest.mark.parametrize("false_key", sorted(ALL_TRUE))
def test_any_false_attestation_is_422_with_reasons(fresh_db, client, false_key):
    make_ready(client)
    body = {"attestations": {**ALL_TRUE, false_key: False}}
    r = client.post(BUILD, json=body)
    assert r.status_code == 422, r.text
    reasons = reasons_of(r)
    assert isinstance(reasons, list) and len(reasons) == 1
    assert all(isinstance(reason, str) and reason for reason in reasons)


@pytest.mark.parametrize(
    "send_body",
    [True, False],
    ids=["empty-object", "no-body"],
)
def test_missing_attestations_is_422_listing_all_three(fresh_db, client, send_body):
    make_ready(client)
    r = client.post(BUILD, json={}) if send_body else client.post(BUILD)
    assert r.status_code == 422, r.text
    reasons = reasons_of(r)
    assert len(reasons) == 3 and all(isinstance(x, str) and x for x in reasons)


def test_partial_attestations_object_is_422(fresh_db, client):
    make_ready(client)
    r = client.post(BUILD, json={"attestations": {"reviewed_fields": True}})
    assert r.status_code == 422, r.text


def test_422_wins_over_409_when_gaps_open_and_attestation_false(fresh_db, client):
    # Gaps and the tenant gate are both failing here; the body check runs first.
    body = {"attestations": {**ALL_TRUE, "reviewed_fields": False}}
    r = client.post(BUILD, json=body)
    assert r.status_code == 422, r.text
    assert len(reasons_of(r)) == 1


def test_blocking_gaps_open_is_409_with_both_titles(fresh_db, client):
    r = client.post(BUILD, json=ATTESTED)
    assert r.status_code == 409, r.text
    reasons = reasons_of(r)
    assert set(reasons) == {REASON_LANDLORD, REASON_REMEDY}
    assert len(reasons) == 2





def test_tenant_not_attested_is_409(fresh_db, client, session):
    make_ready(client)
    case = session.get(CaseRow, CASE)
    assert case is not None
    case.tenant_attested_genuine = False
    session.add(case)
    session.commit()
    r = client.post(BUILD, json=ATTESTED)
    assert r.status_code == 409, r.text
    assert reasons_of(r) == [REASON_TENANT]


def test_dismissed_gap_does_not_hide_empty_required_field(fresh_db, client):
    # A dismissed gap is no longer open, so the empty field is reported on its own.
    r = client.patch("/api/gaps/gap_01", json={"status": "dismissed"})
    assert r.status_code == 200, r.text
    client.patch("/api/gaps/gap_02", json={"status": "resolved", "value": "Repair the leak"})
    r = client.post(BUILD, json=ATTESTED)
    assert r.status_code == 409, r.text
    assert reasons_of(r) == ["Required field is empty: Landlord address"]


def test_refused_builds_leave_no_trace(fresh_db, client, session):
    packages_before = len(package_rows(session))
    activity_before = len(activity_rows(session))
    unconfirmed = {"attestations": {**ALL_TRUE, "remedy_confirmed_with_tenant": False}}
    assert client.post(BUILD, json=unconfirmed).status_code == 422
    assert client.post(BUILD, json=ATTESTED).status_code == 409
    assert len(package_rows(session)) == packages_before
    assert len(activity_rows(session)) == activity_before
    assert download(client).status_code == 404


# --- success and download ------------------------------------------------------------------


def test_success_returns_package_shape(fresh_db, client):
    make_ready(client)
    body = build(client).json()
    assert set(body) == set(schemas.Package.model_fields)
    assert body["version"] == 1
    assert len(body["sha256"]) == 64 and all(c in "0123456789abcdef" for c in body["sha256"])
    assert body["prepared_by"] == client.get("/api/me").json()["name"]
    assert body["built_at"]
    assert {f["filename"] for f in body["files"]} == ZIP_NAMES
    for f in body["files"]:
        assert set(f) == {"label", "filename", "sha256", "note"}
        assert len(f["sha256"]) == 64 and f["label"] and f["note"]
    assert isinstance(body["reminders"], list) and body["reminders"]
    assert all(isinstance(x, str) and x for x in body["reminders"])


def test_download_is_zip_with_expected_headers_and_hashes(fresh_db, client, session):
    make_ready(client)
    built = build(client).json()
    r = download(client)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert r.headers["content-disposition"] == (
        'attachment; filename="tenantproof-case_demo_001-v1.zip"'
    )
    assert r.headers["x-package-sha256"] == built["sha256"]
    assert int(r.headers["content-length"]) == len(r.content)
    assert sha(r.content) == built["sha256"]
    rows = package_rows(session)
    assert len(rows) == 1 and rows[0].sha256 == sha(r.content)
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        assert set(archive.namelist()) == ZIP_NAMES
        assert len(archive.namelist()) == 3
    entries = unzip(r.content)
    for f in built["files"]:
        assert sha(entries[f["filename"]]) == f["sha256"]


def test_manifest_json_lists_seeded_evidence_with_matching_hashes(fresh_db, client, session):
    make_ready(client)
    build(client)
    entries = unzip(download(client).content)
    manifest = json.loads(entries["manifest.json"])
    assert manifest["case_id"] == CASE
    assert manifest["version"] == 1
    assert set(manifest["documents"]) == PDF_NAMES
    for name in PDF_NAMES:
        assert manifest["documents"][name] == sha(entries[name])

    session.expire_all()
    rows = list(session.exec(
        select(EvidenceRow).where(EvidenceRow.case_id == CASE).order_by(col(EvidenceRow.position))
    ).all())
    assert len(rows) == 7
    assert len(manifest["evidence"]) == 7
    for item, row in zip(manifest["evidence"], rows, strict=True):
        assert item["sha256"] == row.sha256
        assert item["uploaded_by"] == row.uploaded_by
        assert item["label"] == row.label and item["filename"] == row.filename
        assert item["received_at"]
        parsed = datetime.fromisoformat(item["received_at"])
        assert parsed.replace(tzinfo=None) == row.received_at.replace(tzinfo=None)


# --- storage ---------------------------------------------------------------------------


def test_stored_blob_matches_row_and_db_holds_no_zip(fresh_db, client, session):
    make_ready(client)
    built = build(client).json()
    rows = package_rows(session)
    assert len(rows) == 1
    row = rows[0]
    blob = storage.read(row.storage_key)
    assert sha(blob) == row.sha256
    assert row.storage_key == row.sha256
    assert row.size_bytes == len(blob)
    assert blob == download(client).content
    assert row.sha256 == built["sha256"]

    columns = PackageRow.__table__.columns  # type: ignore[attr-defined]
    names = {c.name for c in columns}
    assert not names & {"zip_blob", "data", "zip", "content", "blob", "bytes"}
    assert not any(isinstance(c.type, LargeBinary) for c in columns)


# --- versions --------------------------------------------------------------------------


def test_two_builds_make_two_versions_and_default_serves_latest(fresh_db, client, session):
    make_ready(client)
    first = build(client).json()
    second = build(client).json()
    assert first["version"] == 1 and second["version"] == 2
    assert first["sha256"] != second["sha256"]
    rows = package_rows(session)
    assert [r.version for r in rows] == [1, 2]
    assert rows[0].sha256 == first["sha256"] and rows[1].sha256 == second["sha256"]

    default = download(client)
    assert default.status_code == 200
    assert default.headers["content-disposition"].endswith('-v2.zip"')
    assert sha(default.content) == second["sha256"]

    explicit_latest = download(client, version=2)
    assert explicit_latest.content == default.content

    old = download(client, version=1)
    assert old.status_code == 200
    assert old.headers["content-disposition"].endswith('-v1.zip"')
    assert sha(old.content) == first["sha256"]
    assert old.headers["x-package-sha256"] == first["sha256"]


def test_version_query_edge_cases(fresh_db, client):
    assert download(client).status_code == 404
    assert download(client, version=1).status_code == 404
    make_ready(client)
    build(client)
    assert download(client, version=99).status_code == 404
    assert download(client, version=0).status_code == 422
    assert client.get(DOWNLOAD, params={"version": -1}).status_code == 422
    assert client.get(DOWNLOAD, params={"version": "abc"}).status_code == 422


def test_case_id_version_pair_is_unique_in_database(fresh_db, client, session):
    make_ready(client)
    build(client)
    duplicate = PackageRow(
        id="dup-" + "0" * 28, case_id=CASE, version=1, sha256="0" * 64, storage_key="0" * 64,
        size_bytes=1, prepared_by="nobody", attestations={}, files=[],
    )
    session.add(duplicate)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_rebuild_after_edits_keeps_old_version_bytes(fresh_db, client, session):
    make_ready(client)
    first = build(client).json()
    v1_bytes = download(client, version=1).content
    v1_row = package_rows(session)[0]
    v1_key, v1_row_sha = v1_row.storage_key, v1_row.sha256

    r = client.patch(
        f"/api/cases/{CASE}/form-draft/fields/rental_address",
        json={"value": "Suite 77, 100 Example St"},
    )
    assert r.status_code == 200, r.text
    r = client.patch(
        "/api/timeline/te_09",
        json={"review_status": "accepted", "description": "Tenant asked for repair."},
    )
    assert r.status_code == 200, r.text

    second = build(client).json()
    assert second["version"] == 2 and second["sha256"] != first["sha256"]

    assert download(client, version=1).content == v1_bytes
    assert sha(storage.read(v1_key)) == v1_row_sha == first["sha256"]
    assert "Suite 77" not in pdf_text(unzip(v1_bytes)["application_draft.pdf"])
    assert "Suite 77" in pdf_text(unzip(download(client).content)["application_draft.pdf"])


# --- activity log -----------------------------------------------------------------------


def test_each_build_adds_exactly_one_caseworker_entry_with_its_hash(fresh_db, client, session):
    make_ready(client)
    user_name = client.get("/api/me").json()["name"]
    for version in (1, 2):
        before = len(activity_rows(session))
        built = build(client).json()
        assert built["version"] == version
        assert len(activity_rows(session)) == before + 1
        entry = activity_rows(session)[-1]
        assert entry.actor == "caseworker"
        assert entry.actor_name == user_name
        assert f"v{version}" in entry.action
        assert built["sha256"] in entry.action
    assert len(built_entries(session)) == 2


# --- caseworker_note never leaves the caseworker workspace ------------------------------


def test_caseworker_note_never_appears_in_package(fresh_db, client, session):
    make_ready(client)
    secret = "SECRET-NOTE-XYZ"
    for event_id in ("te_03", "te_02"):
        event = session.get(TimelineEventRow, event_id)
        assert event is not None and event.case_id == CASE
        event.caseworker_note = secret
        session.add(event)
    session.commit()
    session.expire_all()
    # The note is reachable in the caseworker workspace, so the absence below is meaningful.
    timeline = {t["id"]: t for t in client.get(f"/api/cases/{CASE}/timeline").json()}
    assert timeline["te_03"]["caseworker_note"] == secret

    build(client)
    body = download(client).content
    assert secret.encode() not in body
    entries = unzip(body)
    for name, data in entries.items():
        assert secret.encode() not in data, name
    assert secret not in entries["manifest.json"].decode("utf-8")
    for name in PDF_NAMES:
        assert "SECRET" not in pdf_text(entries[name]), name


# --- org isolation and auth -------------------------------------------------------------


def test_other_org_cannot_build_or_download(fresh_db, client, other_org_client, session):
    make_ready(client)
    build(client)
    rows_before = len(package_rows(session))
    assert other_org_client.post(BUILD, json=ATTESTED).status_code == 404
    assert download(other_org_client).status_code == 404
    assert download(other_org_client, version=1).status_code == 404
    assert len(package_rows(session)) == rows_before





def test_anonymous_is_401(fresh_db, anon_client, session):
    before = len(package_rows(session))
    assert anon_client.post(BUILD, json=ATTESTED).status_code == 401
    assert download(anon_client).status_code == 401
    assert download(anon_client, version=1).status_code == 401
    assert len(package_rows(session)) == before


# --- narrative edge cases: month precision, unknown date, zero sources, rejected events ---


def squash(text: str) -> str:
    """Drop every whitespace character. The PDF wraps lines (even mid-word for CJK), so
    substring checks compare squashed text on both sides or a wrapped match is missed."""
    return "".join(text.split())


def application_text(client) -> str:
    entries = unzip(download(client).content)
    return squash(pdf_text(entries["application_draft.pdf"]))


def test_month_precision_event_is_rendered_as_month_only(fresh_db, client):
    make_ready(client)
    r = client.patch("/api/timeline/te_07", json={"review_status": "accepted"})  # 2026-09, month
    assert r.status_code == 200, r.text
    build(client)
    expected = squash("Sep 2026 (month only): Mould appeared on the bathroom ceiling")
    assert expected in application_text(client)


def test_unknown_date_event_is_rendered_as_date_unknown(fresh_db, client):
    make_ready(client)
    marker = "ZETA-UNDATED-MARK"
    r = client.patch(
        "/api/timeline/te_09",
        json={
            "date": {"value": None, "precision": "unknown", "basis": "user"},
            "description": marker,
            "review_status": "accepted",
        },
    )
    assert r.status_code == 200, r.text
    build(client)
    assert squash(f"Date unknown: {marker}") in application_text(client)


def test_rejected_event_never_reaches_the_pdf(fresh_db, client):
    make_ready(client)
    marker = "ZETA-REJECT-MARK"
    r = client.patch(
        "/api/timeline/te_04", json={"description": marker, "review_status": "accepted"}
    )
    assert r.status_code == 200, r.text
    build(client)
    assert squash(marker) in application_text(client)  # control: accepted events render

    r = client.patch("/api/timeline/te_04", json={"review_status": "rejected"})
    assert r.status_code == 200, r.text
    assert r.json()["review_status"] == "rejected"
    build(client)  # rejected is not a blocker
    assert squash(marker) not in application_text(client)
    v1_pdf = unzip(download(client, version=1).content)["application_draft.pdf"]
    assert squash(marker) in squash(pdf_text(v1_pdf))


def test_pending_events_never_reach_the_pdf(fresh_db, client):
    make_ready(client)
    build(client)
    text = application_text(client)
    pending = [t for t in client.get(f"/api/cases/{CASE}/timeline").json()
               if t["review_status"] == "pending"]
    assert pending, "seed should have pending events"
    for event in pending:
        assert squash(event["description"]) not in text


def test_accepted_event_with_zero_sources_is_left_out_of_the_pdf(fresh_db, client, session):
    """No source, no fact. The PATCH refuses to accept such an event; if one gets in another
    way (here: sources wiped on an already-accepted event) the PDF still leaves it out."""
    make_ready(client)
    event = session.get(TimelineEventRow, "te_01")
    assert event is not None
    event.sources = []
    session.add(event)
    session.commit()
    build(client)
    text = application_text(client)
    assert squash("The tenancy began under a written one-year lease.") not in text
    assert squash("Landlord") in text or squash("Tenant") in text  # other events still render
