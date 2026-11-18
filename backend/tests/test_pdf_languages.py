"""Hindi and Russian in the PDFs the app makes (issues #68, #69).

Cyrillic needs no shaping and draws correctly. Devanagari does not: without text shaping
(`uharfbuzz`, not a dependency) vowel signs and conjuncts come out in the wrong place. So a Hindi
case's PDFs show a marker instead of Devanagari text and say so; the original files carry it.
"""

import io
import json
import re
import zipfile

import pytest
from pypdf import PdfReader
from sqlmodel import Session

from app.models import CaseRow
from app.services.package import HINDI_MARK, HINDI_NOTE, _write, new_pdf
from tests.test_evidence_upload import ATTESTATIONS, BUILD, CASE, DOWNLOAD, make_ready, upload

DEVANAGARI = re.compile("[ऀ-ॿ]")
HI_NAME = "प्रिया शर्मा"
HI_EVENT = "मकान मालिक को रिसाव की सूचना दी गई Landlord notified of the leak"
HI_CHAT = "नमस्ते, छत से पानी टपक रहा है। कृपया जल्दी ठीक करवाएँ।\n"
RU_NAME = "Анна Петрова"
RU_EVENT = "Арендодателю сообщили о протечке Landlord notified of the leak"
BRIEF = f"/api/cases/{CASE}/case-brief"


def pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def set_language(session: Session, language: str, name: str) -> None:
    case = session.get(CaseRow, CASE)
    assert case is not None
    case.tenant_language, case.tenant_name = language, name
    session.add(case)
    session.commit()


def build(client, event: str, chat: str) -> tuple[dict[str, bytes], str]:
    """Package entries and the case brief's text, for the demo case with `event` on its timeline
    and `chat` uploaded as a text file."""
    assert upload(client, ("chat.txt", chat.encode(), "text/plain")).status_code == 200
    make_ready(client)
    patched = client.patch("/api/timeline/te_03", json={"description": event,
                                                        "review_status": "accepted"})
    assert patched.status_code == 200, patched.text
    assert client.post(BUILD, json=ATTESTATIONS).status_code == 200
    with zipfile.ZipFile(io.BytesIO(client.get(DOWNLOAD).content)) as archive:
        entries = {n: archive.read(n) for n in archive.namelist()}
    brief = client.get(BRIEF)
    assert brief.status_code == 200, brief.text
    return entries, pdf_text(brief.content)


def all_pdf_text(entries: dict[str, bytes]) -> str:
    return "\n".join(pdf_text(d) for n, d in entries.items() if n.endswith(".pdf"))


def test_a_hindi_case_has_no_devanagari_in_any_pdf_and_says_so(fresh_db, client, session):
    set_language(session, "hi", HI_NAME)
    entries, brief = build(client, HI_EVENT, HI_CHAT)

    text = all_pdf_text(entries)
    assert not DEVANAGARI.search(text) and not DEVANAGARI.search(brief)
    assert HINDI_MARK in text and "Landlord notified of the leak" in text  # English stays
    # the line is on the evidence manifest, the case brief and in manifest.json
    assert HINDI_NOTE in " ".join(pdf_text(entries["evidence_manifest.pdf"]).split())
    assert HINDI_NOTE in " ".join(brief.split())
    assert json.loads(entries["manifest.json"])["notes"] == [HINDI_NOTE]
    # the Hindi chat is filed as it was; no PDF copy of it would read correctly
    assert entries["evidence/E-08.txt"] == HI_CHAT.encode()
    assert "evidence/E-08.pdf" not in entries


def test_a_hindi_case_still_gets_a_pdf_copy_of_an_english_text_file(fresh_db, client, session):
    set_language(session, "hi", HI_NAME)
    entries, _ = build(client, HI_EVENT, "The ceiling is leaking.\n")
    assert "The ceiling is leaking." in pdf_text(entries["evidence/E-08.pdf"])


def test_a_russian_case_keeps_its_cyrillic_and_has_no_note(fresh_db, client, session):
    set_language(session, "ru", RU_NAME)
    entries, brief = build(client, RU_EVENT, "Здравствуйте, с потолка течёт вода.\n")

    text = all_pdf_text(entries)
    assert "Арендодателю сообщили о протечке" in text
    assert "с потолка течёт вода" in pdf_text(entries["evidence/E-08.pdf"])
    assert RU_NAME in brief
    assert HINDI_NOTE not in text + brief and HINDI_MARK not in text + brief
    assert "notes" not in json.loads(entries["manifest.json"])


def test_other_languages_are_unchanged(fresh_db, client, session):
    set_language(session, "es", "Ana Silva")
    entries, brief = build(client, "Landlord notified", "Hola\n")
    assert HINDI_NOTE not in all_pdf_text(entries) + brief
    assert "notes" not in json.loads(entries["manifest.json"])


@pytest.mark.parametrize(("language", "marked"), [("hi", True), ("ru", False), ("zh", False)])
def test_the_marker_is_for_hindi_cases_only(language: str, marked: bool) -> None:
    pdf = new_pdf(language)
    _write(pdf, "Tenant: प्रिया शर्मा")
    text = pdf_text(bytes(pdf.output()))
    assert (HINDI_MARK in text) is marked
    assert (DEVANAGARI.search(text) is None) is marked
