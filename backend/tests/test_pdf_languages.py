"""Hindi and Russian in the PDFs the app makes (issues #68, #69).

Cyrillic needs no shaping. Devanagari uses `uharfbuzz` via fpdf2's `set_text_shaping` with
script `deva`, turned on only for paragraphs that contain Devanagari.
"""

import hashlib
import io
import json
import re
import zipfile

from pypdf import PdfReader
from sqlmodel import Session

from app.models import CaseRow, FormFieldRow
from app.services.package import _write, new_pdf, shape_for
from tests.test_evidence_upload import ATTESTATIONS, BUILD, CASE, DOWNLOAD, make_ready, upload

DEVANAGARI = re.compile("[\u0900-\u097f]")
CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f]")
HI_NAME = "प्रिया शर्मा"
HI_EVENT = "मकान मालिक को रिसाव की सूचना दी गई Landlord notified of the leak"
HI_CHAT = "नमस्ते, छत से पानी टपक रहा है। कृपया जल्दी ठीक करवाएँ।\n"
RU_NAME = "Анна Петрова"
RU_EVENT = "Арендодателю сообщили о протечке Landlord notified of the leak"
OLD_MARK = "[Hindi text, see the original file]"
OLD_NOTE = "PDFs are in English; Hindi PDF rendering is not supported yet."
BRIEF = f"/api/cases/{CASE}/case-brief"


def pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def clean_pdf_text(data: bytes) -> str:
    return CONTROL.sub("", pdf_text(data))


def devanagari_letters(text: str) -> set[str]:
    return set(DEVANAGARI.findall(text))


def set_language(session: Session, language: str, name: str) -> None:
    case = session.get(CaseRow, CASE)
    assert case is not None
    case.tenant_language, case.tenant_name = language, name
    session.add(case)
    field = session.get(FormFieldRow, (CASE, "tenant_name"))
    if field is not None:
        field.value = name
        session.add(field)
    session.commit()


def build(
    client, event: str, chat: str, filename: str = "chat.txt"
) -> tuple[dict, dict[str, bytes], str]:
    """Build response, package entries, and the case brief's text, for the demo case with `event`
    on its timeline and `chat` uploaded as a text file."""
    assert upload(client, (filename, chat.encode(), "text/plain")).status_code == 200
    make_ready(client)
    patched = client.patch("/api/timeline/te_03", json={"description": event,
                                                        "review_status": "accepted"})
    assert patched.status_code == 200, patched.text
    built = client.post(BUILD, json=ATTESTATIONS)
    assert built.status_code == 200, built.text
    with zipfile.ZipFile(io.BytesIO(client.get(DOWNLOAD).content)) as archive:
        entries = {n: archive.read(n) for n in archive.namelist()}
    brief = client.get(BRIEF)
    assert brief.status_code == 200, brief.text
    return built.json(), entries, clean_pdf_text(brief.content)


def all_pdf_text(entries: dict[str, bytes]) -> str:
    return "\n".join(clean_pdf_text(d) for n, d in entries.items() if n.endswith(".pdf"))


def test_a_hindi_case_draws_its_hindi_text_in_every_pdf(fresh_db, client, session):
    set_language(session, "hi", HI_NAME)
    _, entries, brief = build(client, HI_EVENT, HI_CHAT, filename="संदेश-chat.txt")

    app_text = clean_pdf_text(entries["application_draft.pdf"])
    man_text = clean_pdf_text(entries["evidence_manifest.pdf"])
    chat_pdf_text = clean_pdf_text(entries["evidence/E-08.pdf"])
    all_text = "\n".join((app_text, man_text, chat_pdf_text, brief))

    assert devanagari_letters(HI_NAME) <= devanagari_letters(app_text)
    assert devanagari_letters(HI_NAME) <= devanagari_letters(brief)
    assert devanagari_letters(HI_EVENT) <= devanagari_letters(app_text)
    assert devanagari_letters(HI_EVENT) <= devanagari_letters(brief)
    assert "Landlord notified of the leak" in app_text
    assert devanagari_letters(HI_CHAT) <= devanagari_letters(chat_pdf_text)
    assert devanagari_letters("संदेश") <= devanagari_letters(man_text)
    assert "evidence/E-08.pdf" in man_text

    assert OLD_MARK not in all_text
    assert OLD_NOTE not in all_text
    assert "notes" not in json.loads(entries["manifest.json"])


def test_a_hindi_txt_file_gets_its_pdf_copy_with_matching_hash(fresh_db, client, session):
    set_language(session, "hi", HI_NAME)
    built, entries, _ = build(client, HI_EVENT, HI_CHAT)

    assert entries["evidence/E-08.txt"] == HI_CHAT.encode()
    assert "evidence/E-08.pdf" in entries
    expected_sha = hashlib.sha256(entries["evidence/E-08.pdf"]).hexdigest()
    listed = {f["filename"]: f["sha256"] for f in built["files"]}
    assert listed["evidence/E-08.pdf"] == expected_sha
    chat_pdf = clean_pdf_text(entries["evidence/E-08.pdf"])
    assert devanagari_letters(HI_CHAT) <= devanagari_letters(chat_pdf)


def test_an_english_case_with_a_hindi_chat_shapes_the_devanagari(fresh_db, client):
    _, entries, brief = build(client, HI_EVENT, HI_CHAT)

    app_text = clean_pdf_text(entries["application_draft.pdf"])
    chat_pdf_text = clean_pdf_text(entries["evidence/E-08.pdf"])
    assert devanagari_letters(HI_EVENT) <= devanagari_letters(app_text)
    assert devanagari_letters(HI_EVENT) <= devanagari_letters(brief)
    assert devanagari_letters(HI_CHAT) <= devanagari_letters(chat_pdf_text)


def test_shape_for_turns_shaping_on_only_for_devanagari():
    pdf = new_pdf()
    assert pdf.text_shaping is None

    assert shape_for(pdf, HI_NAME) == HI_NAME
    assert pdf.text_shaping is not None
    assert pdf.text_shaping["use_shaping_engine"] is True
    assert pdf.text_shaping["script"] == "deva"

    for plain in ("Jordan Lee", "Анна Петрова", "张伟"):
        assert shape_for(pdf, plain) == plain
        assert pdf.text_shaping is None


def test_four_probe_lines_and_edge_cases_render():
    probes = [
        ("हिन्दी", set("हनद")),
        ("Note: हिन्दी text", set("हनद")),
        ("हिन्दी and English", set("हनद")),
        ("中文 हिन्दी", set("हनद")),
        ("हिन्दी и Русский", set("हनद")),
    ]
    pdf = new_pdf()
    for line, _ in probes:
        _write(pdf, line)
    _write(pdf, HI_EVENT, bold=True)
    _write(pdf, (HI_CHAT.strip() + " ") * 60)
    _write(pdf, "क" * 120)

    extracted = clean_pdf_text(bytes(pdf.output()))
    lines = extracted.splitlines()
    for index, (_, consonants) in enumerate(probes):
        assert consonants <= devanagari_letters(lines[index])
    assert devanagari_letters(HI_EVENT) <= devanagari_letters(extracted)


def test_a_russian_case_keeps_its_cyrillic_and_has_no_note(fresh_db, client, session):
    set_language(session, "ru", RU_NAME)
    _, entries, brief = build(client, RU_EVENT, "Здравствуйте, с потолка течёт вода.\n")

    text = all_pdf_text(entries)
    assert "Арендодателю сообщили о протечке" in text
    assert "с потолка течёт вода" in pdf_text(entries["evidence/E-08.pdf"])
    assert RU_NAME in brief
    assert OLD_NOTE not in text + brief and OLD_MARK not in text + brief
    assert "notes" not in json.loads(entries["manifest.json"])


def test_other_languages_are_unchanged(fresh_db, client, session):
    set_language(session, "es", "Ana Silva")
    _, entries, brief = build(client, "Landlord notified", "Hola\n")
    assert OLD_NOTE not in all_pdf_text(entries) + brief
    assert "notes" not in json.loads(entries["manifest.json"])
