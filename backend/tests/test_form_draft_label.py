"""The Form J draft covers seven tenant-applicant fields, so wherever it shows it says so, and the
two legal sources checked in issue #54 are confirmed by a person."""

import io

from pypdf import PdfReader
from sqlmodel import Session

from app.models import CaseRow
from app.services.legal import corpus
from tests.test_package import ATTESTED, BUILD, DOWNLOAD, make_ready, unzip

LABEL = "Partial Form J draft: caseworker completes the rest"
CASE = "case_demo_001"


def test_the_label_is_the_one_the_ui_and_the_package_use():
    assert corpus.FORM_J_DRAFT_LABEL == LABEL


def test_the_form_draft_endpoint_carries_the_label(fresh_db, client):
    assert client.get(f"/api/cases/{CASE}/form-draft").json()["form_name"] == LABEL


def test_an_old_row_with_the_old_name_reads_with_the_label(fresh_db, client, session: Session):
    case = session.get(CaseRow, CASE)
    assert case is not None
    case.form_name = "Application to Director (Form J) — draft"
    session.add(case)
    session.commit()
    assert client.get(f"/api/cases/{CASE}/form-draft").json()["form_name"] == LABEL


def test_a_case_with_no_form_name_stays_blank(fresh_db, client, session: Session):
    case = session.get(CaseRow, CASE)
    assert case is not None
    case.form_name = ""
    session.add(case)
    session.commit()
    assert client.get(f"/api/cases/{CASE}/form-draft").json()["form_name"] == ""


def test_a_new_case_gets_the_label(fresh_db, client):
    body = {"tenant_name": "Ana Silva", "tenant_language": "es", "unit": "Unit 2",
            "title": "No hot water"}
    made = client.post("/api/cases", json=body).json()
    assert client.get(f"/api/cases/{made['id']}/form-draft").json()["form_name"] == LABEL


def test_the_application_pdf_in_the_package_is_titled_with_the_label(fresh_db, client):
    make_ready(client)
    assert client.post(BUILD, json=ATTESTED).status_code == 200
    entries = unzip(client.get(DOWNLOAD).content)
    reader = PdfReader(io.BytesIO(entries["application_draft.pdf"]))
    text = " ".join(" ".join(p.extract_text() for p in reader.pages).split())
    assert LABEL in text


def test_every_legal_source_is_confirmed_by_a_person_with_a_date():
    for entry in corpus.CORPUS:
        if entry.excerpt:
            assert entry.confirmed_by and entry.confirmed_on, entry.id


def test_the_two_sources_of_issue_54_are_confirmed():
    form_j = corpus.by_id("ns-form-j")
    regs = corpus.by_id("ns-rtr-25a")
    assert form_j is not None and regs is not None
    assert (form_j.confirmed_by, form_j.confirmed_on) == ("Lameda12", "2026-11-17")
    assert (regs.confirmed_by, regs.confirmed_on) == ("Lameda12", "2026-11-17")
    assert form_j.excerpt == (
        "Form J: Application to Director (Section 13 of the Residential Tenancies Act)"
    )


def test_the_form_j_entry_says_the_draft_is_partial():
    form_j = corpus.by_id("ns-form-j")
    assert form_j is not None
    assert "partial draft" in form_j.used_for and "caseworker completes" in form_j.used_for
    assert len(form_j.form_fields) == 7
