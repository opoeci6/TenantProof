"""Photo previews for the caseworker (#97): GET /cases/{case_id}/evidence/{evidence_id}/preview
serves only the copy made at upload without EXIF or GPS, never the original."""

import io
import shutil

import pytest
from PIL import Image

from app.services import storage
from tests.test_empty_states import js
from tests.test_evidence_upload import (
    CASE,
    EVIDENCE,
    GPS_IFD,
    evidence_rows,
    heic_with_gps,
    jpeg_with_gps,
    pdf_with_text,
    upload,
)  # fmt: skip

OTHER_CASE = "case_002"  # same org as the demo case


def preview(evidence_id: str, case: str = CASE) -> str:
    return f"/api/cases/{case}/evidence/{evidence_id}/preview"


def png_bytes() -> bytes:
    exif = Image.Exif()
    exif.get_ifd(GPS_IFD)[1] = "N"
    out = io.BytesIO()
    Image.new("RGBA", (40, 20), (0, 90, 200, 255)).save(out, "PNG", exif=exif.tobytes())
    return out.getvalue()


def no_metadata(data: bytes) -> bool:
    with Image.open(io.BytesIO(data)) as img:
        return len(img.getexif()) == 0 and not img.getexif().get_ifd(GPS_IFD)


def test_a_photo_preview_is_the_copy_without_gps(fresh_db, client, session) -> None:
    original = jpeg_with_gps()
    [item] = upload(client, ("IMG_0001.jpg", original, "image/jpeg")).json()
    assert item["has_preview"] is True

    r = client.get(preview(item["id"]))

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "private, no-cache"
    assert r.headers["content-disposition"] == "inline"
    assert r.headers["x-content-type-options"] == "nosniff"
    row = evidence_rows(session)[-1]
    assert r.content == storage.read(row.stripped_sha256)  # type: ignore[arg-type]
    assert r.content != original
    assert no_metadata(r.content)
    assert b"iPhone" not in r.content and b"Lightroom" not in r.content
    assert r.headers["etag"] == f'"{row.stripped_sha256}"'


def test_an_unchanged_copy_answers_304_only_after_the_login_check(
    fresh_db, client, other_org_client, anon_client
) -> None:
    [item] = upload(client, ("IMG_0004.jpg", jpeg_with_gps((4, 4, 4)), "image/jpeg")).json()
    etag = client.get(preview(item["id"])).headers["etag"]
    again = {"If-None-Match": etag}

    r = client.get(preview(item["id"]), headers=again)
    assert r.status_code == 304 and r.content == b""
    assert r.headers["etag"] == etag and r.headers["cache-control"] == "private, no-cache"
    assert client.get(preview(item["id"]), headers={"If-None-Match": '"other"'}).status_code == 200
    assert anon_client.get(preview(item["id"]), headers=again).status_code == 401
    assert other_org_client.get(preview(item["id"]), headers=again).status_code == 404


def test_a_png_stays_a_png(fresh_db, client) -> None:
    [item] = upload(client, ("screen.png", png_bytes(), "image/png")).json()
    r = client.get(preview(item["id"]))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert no_metadata(r.content)


def test_a_heic_is_previewed_as_its_jpeg_copy(fresh_db, client) -> None:
    [item] = upload(client, ("IMG_4412.HEIC", heic_with_gps(), "image/heic")).json()
    r = client.get(preview(item["id"]))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(r.content)) as img:
        assert img.format == "JPEG"
    assert no_metadata(r.content)


def test_files_without_a_clean_copy_have_no_preview(fresh_db, client) -> None:
    items = upload(
        client,
        ("lease.pdf", pdf_with_text(["Lease"]), "application/pdf"),
        ("broken.jpg", b"\xff\xd8\xff" + b"garbage that is no jpeg" * 4, "image/jpeg"),
    ).json()
    for item in items:
        assert item["has_preview"] is False
        assert client.get(preview(item["id"])).status_code == 404


def test_seeded_items_have_no_preview(fresh_db, client) -> None:
    """The seed stores no files, so no seeded item offers a preview it can't serve."""
    items = client.get(EVIDENCE).json()
    assert items and all(item["has_preview"] is False for item in items)
    assert client.get(preview(items[0]["id"])).status_code == 404


def test_a_missing_copy_is_a_404_not_a_500(fresh_db, client, session) -> None:
    [item] = upload(client, ("IMG_0002.jpg", jpeg_with_gps((1, 2, 3)), "image/jpeg")).json()
    row = evidence_rows(session)[-1]
    row.stripped_sha256 = "0" * 64
    session.add(row)
    session.commit()
    assert client.get(preview(item["id"])).status_code == 404


def test_only_this_orgs_caseworkers_see_it(
    fresh_db, client, other_org_client, anon_client
) -> None:
    [item] = upload(client, ("IMG_0003.jpg", jpeg_with_gps((9, 9, 9)), "image/jpeg")).json()
    assert other_org_client.get(preview(item["id"])).status_code == 404
    assert anon_client.get(preview(item["id"])).status_code == 401
    # The right org, but the item is in another case: also 404.
    assert client.get(preview(item["id"], OTHER_CASE)).status_code == 404
    assert client.get(preview("ev_nope")).status_code == 404


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
def test_the_page_asks_for_a_preview_only_when_there_is_one() -> None:
    url = "/cases/case_demo_001/evidence/ev_9/preview"
    assert js('previewPath("case_demo_001", {id: "ev_9", has_preview: true})') == url
    assert js('previewPath("case_demo_001", {id: "ev_9", has_preview: false})') is None
    assert js('previewPath("case_demo_001", {id: "ev_9"})') is None  # mock items
    assert js('previewPath("case_demo_001", undefined)') is None  # a source with no item
    assert js('previewPath(null, {id: "ev_9", has_preview: true})') is None
