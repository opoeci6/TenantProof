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
from app.models import ActivityRow, CaseRow, EvidenceRow, FormFieldRow, PackageRow, TimelineEventRow
from app.services import storage

CASE = "case_demo_001"
BUILD = f"/api/cases/{CASE}/package"
INFO = BUILD  # #200: GET reads the built package back
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


def test_landlord_manual_pdf_stays_separate_from_tenant_package(
    fresh_db, client, anon_client, other_org_client,
):
    created = client.post("/api/cases", json={
        "applicant_role": "landlord",
        "tenant_name": "Morgan Test",
        "tenant_language": "en",
        "unit": "4 Example Street, Halifax",
        "title": "Landlord manual draft",
    })
    assert created.is_success, created.text
    base = f"/api/cases/{created.json()['id']}"
    draft_pdf = f"{base}/form-draft/pdf"
    assert anon_client.get(draft_pdf).status_code == 401
    assert other_org_client.get(draft_pdf).status_code == 404
    assert client.get(f"/api/cases/{CASE}/form-draft/pdf").status_code == 409
    assert client.post(f"{base}/runs").status_code == 409
    for blocked in (
        client.post(f"{base}/package"),
        client.post(f"{base}/package", json=ATTESTED),
        client.get(f"{base}/package"),  # #200
        client.get(f"{base}/package/download"),
        client.get(f"{base}/package/files/application_draft.pdf"),
    ):
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["reasons"] == [
            "Landlord cases use the manual Form J draft download."
        ]

    values = {
        "landlord_name": "Example Landlord",
        "tenant_email": "respondent@example.test",
        "landlord_email": "applicant@example.test",
        "landlord_request_termination_other": "yes",
        "landlord_request_termination_other_reason": "A caseworker recorded the reason.",
        "landlord_request_unpaid_rent": "yes",
        "landlord_request_unpaid_rent_reason": "The rent ledger is incomplete.",
    }
    for key, value in values.items():
        saved = client.patch(f"{base}/form-draft/fields/{key}", json={"value": value})
        assert saved.is_success, saved.text

    filename = "Landlord evidence original.txt"
    evidence = b"Caseworker evidence for the landlord case."
    upload = client.post(f"{base}/evidence", files=[("file", (filename, evidence, "text/plain"))])
    assert upload.is_success, upload.text
    landlord_item, = upload.json()

    other_filename = "Another case evidence.txt"
    other_evidence = b"Caseworker evidence on a different case in the same organization."
    upload = client.post(
        f"/api/cases/{CASE}/evidence",
        files=[("file", (other_filename, other_evidence, "text/plain"))],
    )
    assert upload.is_success, upload.text

    draft = client.get(draft_pdf)
    assert draft.is_success, draft.text
    assert draft.headers["content-type"] == "application/pdf"
    assert draft.headers["cache-control"] == "private, no-store"
    assert draft.headers["content-disposition"].endswith('-draft.pdf"')
    text = pdf_text(draft.content)
    assert "Landlord applicant" in text and "Tenant respondent" in text
    assert "Summary of events" not in text
    assert "Evidence index" in text
    assert landlord_item["label"] in text and filename in text and sha(evidence) in text
    assert other_filename not in text and sha(other_evidence) not in text
    assert "Landlord requests" in text and "Tenant requests" not in text
    assert values["landlord_request_termination_other_reason"] in text
    assert "Unpaid rent" not in text and values["landlord_request_unpaid_rent_reason"] not in text
    assert values["tenant_email"] in text and values["landlord_email"] not in text
    assert "Complete the signature and date manually on the official Form J." in text

    for key in ("landlord_request_payment", "applicant_email_consent"):
        saved = client.patch(f"{base}/form-draft/fields/{key}", json={"value": "yes"})
        assert saved.is_success, saved.text
    updated = client.get(draft_pdf)
    assert updated.is_success, updated.text
    text = pdf_text(updated.content)
    assert "Unpaid rent" in text and values["landlord_request_unpaid_rent_reason"] in text
    assert "Payment of money" in text and "(reason not filled in)" not in text
    assert values["tenant_email"] in text and values["landlord_email"] in text

    saved = client.patch(
        f"{base}/form-draft/fields/landlord_request_unpaid_rent", json={"value": ""},
    )
    assert saved.is_success, saved.text
    updated = client.get(draft_pdf)
    assert updated.is_success, updated.text
    text = pdf_text(updated.content)
    assert "Payment of money" in text and "(reason not filled in)" in text
    assert "Unpaid rent" not in text and values["landlord_request_unpaid_rent_reason"] not in text

    make_ready(client)
    build(client)
    application = unzip(download(client).content)["application_draft.pdf"]
    assert "Summary of events" in pdf_text(application)


def test_downloaded_application_and_text_evidence_keep_words_intact(fresh_db, client):
    make_ready(client)
    # Exercise several line boundaries at the normal font sizes in both PDF templates.
    remedy = " ".join(["The leak has affected Unit 2 since the morning."] * 12)
    evidence = " ".join(["Please see the texts attached separately for the report."] * 12)
    saved = client.patch("/api/gaps/gap_02", json={"status": "resolved", "value": remedy})
    assert saved.status_code == 200, saved.text
    uploaded = client.post(
        f"/api/cases/{CASE}/evidence",
        files=[("file", ("messages.txt", evidence.encode(), "text/plain"))],
    )
    assert uploaded.status_code == 200, uploaded.text

    build(client)
    entries = unzip(download(client).content)
    manifest = json.loads(entries["manifest.json"])
    evidence_entry = next(
        item for item in manifest["evidence"] if item["filename"] == "messages.txt"
    )
    assert evidence_entry["rendition"] is not None
    evidence_pdf = evidence_entry["rendition"]["path"]
    assert evidence_pdf.endswith(".pdf")
    assert remedy in " ".join(pdf_text(entries["application_draft.pdf"]).split())
    assert evidence in " ".join(pdf_text(entries[evidence_pdf]).split())


def test_application_pdf_labels_rent_with_currency(fresh_db, client):
    make_ready(client)
    r = client.patch(f"/api/cases/{CASE}/form-draft/fields/monthly_rent", json={"value": "1200"})
    assert r.status_code == 200, r.text
    build(client)
    entries = unzip(download(client).content)
    assert "CAD 1,200.00" in pdf_text(entries["application_draft.pdf"])


def test_application_pdf_groups_f1_addresses_and_respects_email_consent(fresh_db, client):
    make_ready(client)
    form_fields = f"/api/cases/{CASE}/form-draft/fields"
    applicant_email = "f1-applicant@example.test"
    respondent_email = "f1-respondent@example.test"
    values = {
        "tenant_civic_street": "12 Sea Way",
        "tenant_civic_apartment": "5B",
        "tenant_civic_city": "Halifax",
        "tenant_civic_province": "NS",
        "tenant_civic_postal_code": "B3H 1A1",
        "landlord_mailing_street": "15 Sample Road",
        "tenant_home_phone": "902-555-0103",
        "tenant_email": applicant_email,
        "landlord_email": respondent_email,
        "written_lease": "yes",
    }
    for key, value in values.items():
        r = client.patch(f"{form_fields}/{key}", json={"value": value})
        assert r.status_code == 200, r.text

    consent_url = f"{form_fields}/applicant_email_consent"
    for consent in (None, "yes", "no"):
        if consent is not None:
            r = client.patch(consent_url, json={"value": consent})
            assert r.status_code == 200, r.text
        build(client)
        text = pdf_text(unzip(download(client).content)["application_draft.pdf"])
        assert "Tenant civic address" in text and "12 Sea Way" in text
        assert "Apartment or unit 5B" in text and "Halifax, NS B3H 1A1" in text
        assert "Landlord mailing address" in text and "15 Sample Road" in text
        assert "Tenant mailing address" not in text
        assert "902-555-0103" in text and respondent_email in text
        assert (applicant_email in text) == (consent == "yes")
        consent_label = "Consent to email from Service Nova Scotia"
        assert (consent_label in text) == (consent is not None)
        assert text.index("Tenant civic address") < text.index("Written lease")


def test_application_pdf_exports_f2_and_accepts_weekly_rent(fresh_db, client):
    make_ready(client)
    values = {
        "monthly_rent": "",
        "weekly_rent": "350.50",
        "tenancy_start": "",
        "move_in_date": "2026-05-03",
        "fixed_term_start": "2026-05-01",
        "fixed_term_end": "2030-05-01",
        "lease_term": "fixed_term",
        "deposit_paid": "yes",
        "deposit_amount": "500",
    }
    for key, value in values.items():
        r = client.patch(f"/api/cases/{CASE}/form-draft/fields/{key}", json={"value": value})
        assert r.status_code == 200, r.text

    build(client)
    text = pdf_text(unzip(download(client).content)["application_draft.pdf"])
    for value in (
        "Weekly rent", "CAD 350.50", "Move-in date", "2026-05-03",
        "Fixed-term lease start date", "2026-05-01", "Fixed-term lease end date", "2030-05-01",
        "Fixed term", "Security deposit amount", "CAD 500.00",
    ):
        assert value in text
    assert "Monthly rent" not in text


def test_application_pdf_exports_only_selected_f3_requests_and_leaves_signature_manual(
    fresh_db, client,
):
    make_ready(client)
    form_fields = f"/api/cases/{CASE}/form-draft/fields"
    values = {
        "request_repairs": "yes",
        "request_repairs_reason": "Replace the broken ceiling tiles.",
        "request_compliance": "yes",
        "request_other_reason": "Unselected reason stays off the export.",
        "request_rent_relief": "yes",
        "request_rent_relief_reason": "Lower rent for loss of a room.",
        "request_expense_compensation": "yes",
    }
    for key, value in values.items():
        r = client.patch(f"{form_fields}/{key}", json={"value": value})
        assert r.status_code == 200, r.text

    for payment_selected in (False, True):
        if payment_selected:
            r = client.patch(f"{form_fields}/request_payment", json={"value": "yes"})
            assert r.status_code == 200, r.text
        build(client)
        text = pdf_text(unzip(download(client).content)["application_draft.pdf"])
        assert "Tenant requests" in text
        assert "Repairs" in text and values["request_repairs_reason"] in text
        assert "Compliance with a lease or the Act" in text
        assert "(reason not filled in)" in text
        assert values["request_other_reason"] not in text
        assert ("Payment of money" in text) == payment_selected
        assert ("Relief from rent owing" in text) == payment_selected
        assert (values["request_rent_relief_reason"] in text) == payment_selected
        assert ("Compensation for expenses" in text) == payment_selected
        assert "Complete the signature and date manually on the official Form J." in text


def test_application_pdf_omits_details_marked_inapplicable(fresh_db, client):
    make_ready(client)
    for key, value in {
        "notice_given_by": "landlord",
        "notice_given": "no",
        "fixed_term_end": "2030-05-01",
        "lease_term": "month_to_month",
        "deposit_amount": "500",
        "deposit_paid": "no",
    }.items():
        response = client.patch(f"/api/cases/{CASE}/form-draft/fields/{key}", json={"value": value})
        assert response.status_code == 200, response.text

    build(client)
    text = pdf_text(unzip(download(client).content)["application_draft.pdf"])
    assert "Month to month" in text
    assert "Notice given by" not in text
    assert "Fixed-term lease end date" not in text
    assert "CAD 500.00" not in text
    draft = client.get(f"/api/cases/{CASE}/form-draft").json()
    saved_amount = next(field for field in draft["fields"] if field["key"] == "deposit_amount")
    assert saved_amount["value"] == "500"


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


def test_new_package_warns_about_reversed_legacy_dates(fresh_db, client, session):
    make_ready(client)
    fields = [
        ("move_in_date", "2026-04-03"),
        ("move_out_date", "2026-04-04"),
        ("fixed_term_start", "2026-05-02"),
        ("fixed_term_end", "2026-05-03"),
    ]
    for key, value in fields:
        result = client.patch(f"/api/cases/{CASE}/form-draft/fields/{key}", json={"value": value})
        assert result.is_success, result.text
    original = build(client).json()
    original_zip = download(client, version=original["version"]).content
    for key, value in [("move_out_date", "2026-04-01"), ("fixed_term_end", "2026-05-01")]:
        field = session.get(FormFieldRow, (CASE, key))
        assert field is not None
        field.value = value
        session.add(field)
    session.commit()

    latest = build(client).json()
    latest_zip = unzip(download(client, version=latest["version"]).content)
    text = pdf_text(latest_zip["application_draft.pdf"])
    assert "Move-out date cannot be before move-in date" in text
    assert "Fixed-term end date cannot be before its start date" in text
    for value in ("2026-04-03", "2026-04-01", "2026-05-02", "2026-05-01"):
        assert value in text
    assert download(client, version=original["version"]).content == original_zip


# --- #200: reading a built package back --------------------------------------------------


def test_the_built_package_can_be_read_back(fresh_db, client):
    """The Package page lost its contents and file links on reload: GET returns the Package."""
    missing = client.get(INFO)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Package not found"

    make_ready(client)
    first = build(client).json()
    read = client.get(INFO)
    assert read.status_code == 200
    assert read.json() == first
    assert read.headers["cache-control"] == "private, no-store"

    client.patch(f"/api/cases/{CASE}/form-draft/fields/rental_address", json={"value": "Suite 99"})
    second = build(client).json()
    assert client.get(INFO).json() == second
    assert client.get(INFO, params={"version": 1}).json() == first
    assert client.get(INFO, params={"version": 2}).json() == second


def test_an_unknown_version_or_another_org_is_404(fresh_db, client, other_org_client):
    make_ready(client)
    build(client)
    unknown = client.get(INFO, params={"version": 99})
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Package not found"
    assert other_org_client.get(INFO).status_code == 404
    assert other_org_client.get(INFO, params={"version": 1}).status_code == 404


def test_a_version_below_one_is_422(fresh_db, client):
    make_ready(client)
    build(client)
    for bad in (0, -1):
        assert client.get(INFO, params={"version": bad}).status_code == 422
    assert client.get(INFO, params={"version": "abc"}).status_code == 422


def test_reading_the_package_is_not_logged(fresh_db, client, session):
    make_ready(client)
    build(client)
    before = len(activity_rows(session))
    assert client.get(INFO).status_code == 200
    assert client.get(INFO, params={"version": 1}).status_code == 200
    assert len(activity_rows(session)) == before


def test_anonymous_cannot_read_the_package(fresh_db, client, anon_client):
    make_ready(client)
    build(client)
    assert anon_client.get(INFO).status_code == 401


def test_the_page_reads_the_package_back_when_it_loads():
    """LOADERS.pkg restores S.pkg, and the contents table follows the version shown above it."""
    from tests.test_empty_states import INDEX

    source = INDEX.read_text(encoding="utf-8")
    loaders = source[source.index("const LOADERS = {"):source.index("function prioritizeClient(")]
    assert "pkg: async () => {" in loaders
    assert '"GET", `${C}/package`' in loaders
    assert "S.pkg && latest && S.pkg.version === latest.version" in source
