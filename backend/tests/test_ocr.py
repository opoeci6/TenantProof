"""OCR on this server (RapidOCR): text in photos and in scanned PDF pages becomes evidence text the
agent can cite, under locators that say it was machine-read. These tests run the real engine
(its models ship in the wheel; nothing is downloaded). Misreads of scripts the model doesn't know
(Russian, Hindi) are dropped, and nothing in OCR can fail an upload."""

import io
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont
from sqlmodel import Session, col, select

from app.config import get_settings
from app.models import EvidencePageRow, EvidenceRow
from app.services.evidence import exif, ingest, ocr, text

FONTS = Path(__file__).resolve().parents[1] / "app" / "assets" / "fonts"
CASE = "case_demo_001"
EVIDENCE = f"/api/cases/{CASE}/evidence"
EN = "The heater has been broken since October 3."
ZH = "暖气从十月三号就坏了，房东说下周修。"


def picture(*lines: tuple[str, str], size: tuple[int, int] = (1080, 400)) -> Image.Image:
    """Black text on white, one (font file, text) per line, like a phone screenshot."""
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    for n, (font, line) in enumerate(lines):
        draw.text((30, 40 + n * 90), line, font=ImageFont.truetype(str(FONTS / font), 40),
                  fill="black")
    return img


def png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def scanned_pdf(*pages: Image.Image) -> bytes:
    """A PDF whose pages are images only, with no text layer: what a scanner makes."""
    out = io.BytesIO()
    pages[0].save(out, "PDF", save_all=True, append_images=list(pages[1:]), resolution=150)
    return out.getvalue()


@pytest.fixture
def ocr_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "ocr_enabled", True)


def pages_of(session: Session, row_id: str) -> dict[str, str]:
    session.expire_all()
    rows = session.exec(
        select(EvidencePageRow).where(EvidencePageRow.evidence_id == row_id)
        .order_by(col(EvidencePageRow.position))
    ).all()
    return {r.locator: r.text for r in rows}


def uploaded(session: Session, filename: str) -> EvidenceRow:
    session.expire_all()
    return session.exec(
        select(EvidenceRow).where(EvidenceRow.case_id == CASE, EvidenceRow.filename == filename)
    ).one()


# --- the engine and its filters --------------------------------------------------------------


def test_reads_english_and_chinese_lines_in_order() -> None:
    img = picture(("NotoSans-Regular.ttf", EN), ("NotoSansSC-Regular.ttf", ZH))
    assert ocr.read_text(img) == f"{EN}\n{ZH}"


@pytest.mark.parametrize("line", [
    "Отопление не работает с 3 октября.",  # Cyrillic: the model returns punctuation
    "कृपया मकान मालिक का पता भेजें।",  # Devanagari: low-confidence Latin guesses
])
def test_text_the_model_cannot_read_is_dropped_not_guessed(line: str) -> None:
    assert ocr.read_text(picture(("NotoSans-Regular.ttf", line))) == ""


def test_an_image_without_text_gives_nothing() -> None:
    assert ocr.read_text(Image.new("RGB", (800, 600), (120, 160, 200))) == ""


def test_the_callers_image_is_not_changed() -> None:
    img = picture(("NotoSans-Regular.ttf", EN), size=(3000, 400))
    ocr.read_text(img)
    assert img.size == (3000, 400) and img.mode == "RGB"


def test_the_budget_runs_out() -> None:
    assert ocr.Budget(60).left()
    assert not ocr.Budget(0).left()


# --- photos ------------------------------------------------------------------------------------


def test_a_screenshot_upload_gets_its_text_as_a_cited_page(fresh_db, client, session, ocr_on):
    data = png(picture(("NotoSansSC-Regular.ttf", ZH)))
    r = client.post(EVIDENCE, files=[("file", ("landlord_text.png", data, "image/png"))])
    assert r.status_code == 200, r.text
    row = uploaded(session, "landlord_text.png")
    pages = pages_of(session, row.id)
    assert list(pages) == [exif.PHOTO_LOCATOR, ingest.PHOTO_OCR_LOCATOR]  # capture time first
    assert pages[ingest.PHOTO_OCR_LOCATOR] == ZH
    assert row.processing == "done" and row.error is None


def test_with_ocr_off_a_photo_gets_no_text_page(fresh_db, client, session) -> None:
    data = png(picture(("NotoSans-Regular.ttf", EN)))
    client.post(EVIDENCE, files=[("file", ("off.png", data, "image/png"))])
    assert ingest.PHOTO_OCR_LOCATOR not in pages_of(session, uploaded(session, "off.png").id)


def test_photos_after_the_budget_are_kept_and_say_so() -> None:
    done = ingest._process(png(picture(("NotoSans-Regular.ttf", EN))), "image/png",
                           ocr.Budget(0))
    assert done.processing == "done" and done.error == ingest.OCR_SKIPPED
    assert [p["locator"] for p in done.pages] == [exif.PHOTO_LOCATOR]  # capture time only
    assert done.stripped_sha256 is not None


def test_an_ocr_error_never_fails_the_upload(monkeypatch) -> None:
    def broken(img: Image.Image) -> str:
        raise RuntimeError("engine crashed")

    monkeypatch.setattr(ocr, "read_text", broken)
    done = ingest._process(png(picture(("NotoSans-Regular.ttf", EN))), "image/png",
                           ocr.Budget(60))
    assert done.processing == "done" and done.error is None
    assert all(p["locator"] != ingest.PHOTO_OCR_LOCATOR for p in done.pages)


# --- scanned PDFs ------------------------------------------------------------------------------


def test_a_scanned_page_is_found_only_when_asked() -> None:
    data = scanned_pdf(picture(("NotoSans-Regular.ttf", EN)))
    assert text.read_pdf(data).scans == {}  # the legal source check doesn't want them
    scans = text.read_pdf(data, scans=True).scans
    assert list(scans) == ["page 1"]
    with Image.open(io.BytesIO(scans["page 1"])) as img:
        assert img.format == "PNG" and img.mode == "L"


def test_a_page_with_a_text_layer_is_not_a_scan() -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text="Notice of entry: the landlord will enter on Monday at 10 a.m.")
    assert text.read_pdf(bytes(pdf.output()), scans=True).scans == {}


def test_at_most_max_scans_pages_are_taken() -> None:
    blank = picture(("NotoSans-Regular.ttf", "x"))
    data = scanned_pdf(*[blank] * (text.MAX_SCANS + 2))
    assert len(text.read_pdf(data, scans=True).scans) == text.MAX_SCANS


def test_a_scanned_pdf_upload_reads_its_pages(fresh_db, client, session, ocr_on) -> None:
    data = scanned_pdf(picture(("NotoSans-Regular.ttf", EN)),
                       picture(("NotoSansSC-Regular.ttf", ZH)))
    r = client.post(EVIDENCE, files=[("file", ("scan.pdf", data, "application/pdf"))])
    assert r.status_code == 200, r.text
    pages = pages_of(session, uploaded(session, "scan.pdf").id)
    assert pages == {"page 1 (OCR)": EN, "page 2 (OCR)": ZH}


def test_scans_after_the_budget_keep_their_empty_page_and_say_so() -> None:
    pdf = text.read_pdf(scanned_pdf(picture(("NotoSans-Regular.ttf", EN))), scans=True)
    pages, skipped = ingest._ocr_scans(pdf, ocr.Budget(0))
    assert skipped and pages == pdf.pages


# --- review findings: thin images, a busy engine, the budget, mixed PDFs, OCR off -------------


@pytest.fixture
def no_engine(monkeypatch):
    """Fails the test if OCR reaches the engine."""
    def refuse() -> None:
        raise AssertionError("the engine must not run for this image")

    monkeypatch.setattr(ocr, "_engine", refuse)


@pytest.mark.parametrize("size", [(2000, 1), (1, 2000), (2000, 8), (300, 40000), (5000, 200)])
def test_thin_images_are_refused_before_the_engine(no_engine, size) -> None:
    """RapidOCR scales the short side up without capping the long one: a 2000x1 PNG asked for
    a 900-megapixel buffer. Such shapes are lines or bars, never readable text."""
    assert ocr.read_text(Image.new("RGB", size, "white")) == ""


def test_shapes_that_stay_readable() -> None:
    assert ocr.readable_shape(1080, 1300)  # a screenshot
    assert ocr.readable_shape(1080, 20000)  # a 1:18.5 scroll capture: 108 x 2000 after shrinking
    assert not ocr.readable_shape(1080, 22000)  # 1:20.4
    assert ocr.readable_shape(100, 2000)  # 1:20 is the limit
    assert not ocr.readable_shape(99, 2000)
    assert not ocr.readable_shape(31, 31)


def test_a_thin_scanned_page_never_reaches_the_engine(no_engine) -> None:
    pdf = text.read_pdf(scanned_pdf(Image.new("L", (300, 40000), 255)), scans=True)
    assert list(pdf.scans) == ["page 1"]  # passes the child's checks: 12 MP, short side 300
    pages, skipped = ingest._ocr_scans(pdf, ocr.Budget(60))
    assert pages == pdf.pages and not skipped


def test_a_busy_engine_counts_as_skipped_not_as_a_wait(monkeypatch) -> None:
    img = picture(("NotoSans-Regular.ttf", EN))
    assert ocr._lock.acquire(timeout=5)
    try:
        with pytest.raises(ocr.Busy):
            ocr.read_text(img, wait_s=0.05)
        found, skipped = ingest._ocr(img, ocr.Budget(0.05))
    finally:
        ocr._lock.release()
    assert (found, skipped) == ("", True)


def test_the_budget_counts_ocr_time_only(monkeypatch) -> None:
    budget = ocr.Budget(1.0)
    assert budget.remaining() == 1.0  # nothing spent before OCR runs, whatever else takes time
    def slow(img: Image.Image, wait_s: float) -> str:
        time.sleep(0.1)
        return "seen"

    monkeypatch.setattr(ocr, "read_text", slow)
    assert budget.read(Image.new("RGB", (64, 64))) == "seen"
    assert 0.05 < 1.0 - budget.remaining() < 0.6


def test_a_mixed_pdf_reads_only_its_scanned_page(ocr_on) -> None:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text="Notice of entry: the landlord will enter on Monday at 10 a.m.")
    pdf.add_page()
    pdf.image(picture(("NotoSans-Regular.ttf", EN)), x=10, y=10, w=190)
    done = ingest._process(bytes(pdf.output()), "application/pdf", ocr.Budget(60))
    assert [p["locator"] for p in done.pages] == ["page 1", "page 2 (OCR)"]
    assert done.pages[0]["text"].startswith("Notice of entry")
    assert done.pages[1]["text"] == EN and done.error is None


def test_an_ocr_error_on_a_scan_keeps_the_page(monkeypatch) -> None:
    def broken(img: Image.Image, wait_s: float = 60.0) -> str:
        raise RuntimeError("engine crashed")

    monkeypatch.setattr(ocr, "read_text", broken)
    done = ingest._process(scanned_pdf(picture(("NotoSans-Regular.ttf", EN))),
                           "application/pdf", ocr.Budget(60))
    assert [p["locator"] for p in done.pages] == ["page 1"]
    assert done.processing == "done" and done.error is None


def test_with_ocr_off_a_pdf_decodes_no_page_images(monkeypatch) -> None:
    data = scanned_pdf(picture(("NotoSans-Regular.ttf", EN)))
    assert text.read_pdf_isolated(data).scans == {}  # the child skips the images too
    done = ingest._process(data, "application/pdf", None)
    assert [p["locator"] for p in done.pages] == ["page 1"]


def test_max_scans_counts_pages_tried_not_pages_found(monkeypatch) -> None:
    calls: list[int] = []

    def nothing(page: object) -> None:
        calls.append(1)
        return None

    monkeypatch.setattr(text, "_scan_png", nothing)
    data = scanned_pdf(*[picture(("NotoSans-Regular.ttf", "x"))] * (text.MAX_SCANS + 5))
    assert text.read_pdf(data, scans=True).scans == {}
    assert len(calls) == text.MAX_SCANS


def test_an_endless_budget_waits_without_overflowing() -> None:
    """OCR_REQUEST_SECONDS=inf (pydantic accepts it) must not make the lock wait raise."""
    budget = ocr.Budget(float("inf"))
    assert budget.left() and budget.remaining() == float("inf")
    assert ocr.read_text(picture(("NotoSans-Regular.ttf", EN)), wait_s=float("inf")) == EN
