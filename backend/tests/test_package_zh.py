"""A1: the Chinese-language demo case (tenant_language zh) builds a package whose PDFs render
CJK text with the bundled Noto fonts, and the zip never carries caseworker_note."""

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader
from sqlmodel import Session

from app.models import TimelineEventRow
from app.services.package import _write, new_pdf

CASE = "case_demo_001"
BUILD = f"/api/cases/{CASE}/package"
DOWNLOAD = f"/api/cases/{CASE}/package/download"
FONT_DIR = Path(__file__).resolve().parents[1] / "app" / "assets" / "fonts"
ATTESTED = {"attestations": {
    "reviewed_fields": True,
    "remedy_confirmed_with_tenant": True,
    "explained_not_legal_advice": True,
}}
ZH_LANDLORD = "哈利法克斯港口路12号 Harbour Rd 12"
ZH_EVENT = "房东在十月十日收到漏水通知 Landlord notified of the leak"
SECRET = "SECRET-NOTE-ZH"


# --- helpers -----------------------------------------------------------------------------


def make_ready(client) -> None:
    r1 = client.patch(
        "/api/gaps/gap_01", json={"status": "resolved", "value": "12 Harbour Rd, Halifax"}
    )
    r2 = client.patch(
        "/api/gaps/gap_02", json={"status": "resolved", "value": "Repair the ceiling leak"}
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text


def pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def base_fonts(data: bytes) -> set[str]:
    """BaseFont names of every font in every page's /Resources /Font dictionary."""
    names: set[str] = set()
    for page in PdfReader(io.BytesIO(data)).pages:
        resources: Any = page["/Resources"]
        if "/Font" not in resources:
            continue
        for ref in resources["/Font"].values():
            names.add(str(ref.get_object()["/BaseFont"]))
    return names


def fonts_md_rows() -> dict[str, str]:
    """File name -> sha256 from the table in app/assets/fonts/FONTS.md."""
    rows: dict[str, str] = {}
    for line in (FONT_DIR / "FONTS.md").read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0].endswith(".ttf") and re.fullmatch(r"[0-9a-f]{64}", cells[-1]):
            rows[cells[0]] = cells[-1]
    return rows


# --- pure rendering ------------------------------------------------------------------------





def test_mixed_text_extracts_back_out_of_rendered_pdf():
    pdf = new_pdf()
    _write(pdf, "Landlord 房东 notified on 10 Oct 哈利法克斯 Harbour Rd")
    text = pdf_text(bytes(pdf.output()))
    assert "房东" in text and "哈利法克斯" in text


@pytest.mark.parametrize(
    "name", ["NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "NotoSansSC-Regular.ttf"]
)
def test_bundled_font_file_matches_fonts_md(name):
    rows = fonts_md_rows()
    assert name in rows, f"{name} missing from FONTS.md"
    path = FONT_DIR / name
    assert path.is_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == rows[name]


# --- built package for the Chinese-language case -----------------------------------------


def test_zh_case_package_renders_cjk_with_embedded_noto_fonts(
    fresh_db, client, session: Session
):
    assert client.get(f"/api/cases/{CASE}").json()["tenant"]["preferred_language"] == "zh"
    make_ready(client)
    r = client.patch(
        f"/api/cases/{CASE}/form-draft/fields/landlord_address", json={"value": ZH_LANDLORD}
    )
    assert r.status_code == 200, r.text
    assert r.json()["value"] == ZH_LANDLORD
    r = client.patch("/api/timeline/te_03", json={"description": ZH_EVENT,
                                                  "review_status": "accepted"})
    assert r.status_code == 200, r.text
    event = session.get(TimelineEventRow, "te_03")
    assert event is not None
    event.caseworker_note = SECRET
    session.add(event)
    session.commit()

    built = client.post(BUILD, json=ATTESTED)
    assert built.status_code == 200, built.text
    download = client.get(DOWNLOAD)
    assert download.status_code == 200, download.text
    assert hashlib.sha256(download.content).hexdigest() == built.json()["sha256"]

    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        names = set(archive.namelist())
        entries = {n: archive.read(n) for n in names}
    assert names == {"application_draft.pdf", "evidence_manifest.pdf", "manifest.json"}

    app_pdf = entries["application_draft.pdf"]
    man_pdf = entries["evidence_manifest.pdf"]
    for data in (app_pdf, man_pdf):
        assert len(PdfReader(io.BytesIO(data)).pages) >= 1

    text = pdf_text(app_pdf)
    assert "哈利法克斯" in text, text
    assert "房东" in text, text

    fonts = base_fonts(app_pdf)
    assert any("NotoSansSC" in name for name in fonts), fonts
    # fpdf2 embeds family names with a subset tag, e.g. /MPDFAA+NotoSans or /MPDFAA+NotoSansBold.
    latin = [n for n in fonts if "NotoSans" in n and "NotoSansSC" not in n]
    assert latin and all(re.fullmatch(r"/[A-Z]{6}\+NotoSans(Bold)?", n) for n in latin), fonts

    assert SECRET.encode() not in download.content
    for name, data in entries.items():
        assert SECRET.encode() not in data, name
    assert "SECRET" not in text and "SECRET" not in pdf_text(man_pdf)
    manifest = json.loads(entries["manifest.json"].decode("utf-8"))
    assert SECRET not in json.dumps(manifest, ensure_ascii=False)
