"""Individual file downloads from a built filing package (#91):
GET /cases/{case_id}/package/files/{filename:path}[?version=N]."""

import io
import logging
from pathlib import PurePosixPath
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from PIL import Image

from app.services import package as pkg_svc
from tests.test_empty_states import INDEX
from tests.test_evidence_upload import GPS_IFD, jpeg_with_gps
from tests.test_package import (
    CASE,
    activity_rows,
    build,
    download,
    make_ready,
    package_rows,
    sha,
    unzip,
)

FILES = f"/api/cases/{CASE}/package/files"


def file_url(name: str, case: str = CASE) -> str:
    encoded = "/".join(quote(part, safe="") for part in name.split("/"))
    return f"/api/cases/{case}/package/files/{encoded}"


def get_file(client, name: str, version: int | None = None, case: str = CASE):
    params = {} if version is None else {"version": version}
    return client.get(file_url(name, case=case), params=params)


def upload_evidence(client, filename: str, data: bytes, mime: str) -> dict:
    r = client.post(f"/api/cases/{CASE}/evidence", files=[("file", (filename, data, mime))])
    assert r.status_code == 200, r.text
    return r.json()[0]


def test_each_package_file_downloads_with_its_recorded_hash(fresh_db, client, session):
    """Every entry in POST /package's files list downloads, hashes to its recorded sha256,
    matches the zip member byte-for-byte, and adds no activity entry. Covers TXT + PDF
    rendition and a GPS-bearing JPG (served without GPS, never the original)."""
    make_ready(client)
    original_jpg = jpeg_with_gps()
    txt_bytes = b"2026-09-01 Tenant: The ceiling is leaking again.\n"
    upload_evidence(client, "leak.jpg", original_jpg, "image/jpeg")
    upload_evidence(client, "messages.txt", txt_bytes, "text/plain")

    built = build(client).json()
    zip_entries = unzip(download(client).content)
    activity_before = len(activity_rows(session))

    names = [entry["filename"] for entry in built["files"]]
    jpg_names = [n for n in names if n.startswith("evidence/") and n.endswith(".jpg")]
    txt_names = [n for n in names if n.startswith("evidence/") and n.endswith(".txt")]
    pdf_renditions = [n for n in names if n.startswith("evidence/") and n.endswith(".pdf")]
    assert len(jpg_names) == 1
    assert len(txt_names) == 1
    assert len(pdf_renditions) == 1

    for entry in built["files"]:
        name = entry["filename"]
        r = get_file(client, name)
        assert r.status_code == 200, f"{name}: {r.status_code} {r.text}"
        assert sha(r.content) == entry["sha256"]
        assert r.content == zip_entries[name]

    # The JPG served is the GPS-stripped zip copy, never the original with GPS.
    jpg_resp = get_file(client, jpg_names[0])
    assert jpg_resp.content != original_jpg
    with Image.open(io.BytesIO(jpg_resp.content)) as img:
        assert GPS_IFD not in img.getexif()

    # Reading files changes nothing, so no activity log entry is added.
    assert len(activity_rows(session)) == activity_before


def test_version_query_serves_requested_version_and_defaults_to_latest(fresh_db, client):
    make_ready(client)
    first = build(client).json()
    v1_entries = unzip(download(client, version=1).content)

    r = client.patch(
        f"/api/cases/{CASE}/form-draft/fields/rental_address",
        json={"value": "Suite 99, 200 Example St"},
    )
    assert r.status_code == 200, r.text
    second = build(client).json()
    v2_entries = unzip(download(client, version=2).content)
    assert first["version"] == 1 and second["version"] == 2
    assert v1_entries["application_draft.pdf"] != v2_entries["application_draft.pdf"]

    latest = get_file(client, "application_draft.pdf")
    assert latest.status_code == 200
    assert latest.content == v2_entries["application_draft.pdf"]

    explicit_v2 = get_file(client, "application_draft.pdf", version=2)
    assert explicit_v2.status_code == 200
    assert explicit_v2.content == v2_entries["application_draft.pdf"]

    old = get_file(client, "application_draft.pdf", version=1)
    assert old.status_code == 200
    assert old.content == v1_entries["application_draft.pdf"]
    v1_entry = next(f for f in first["files"] if f["filename"] == "application_draft.pdf")
    assert old.headers["x-file-sha256"] == v1_entry["sha256"]

    assert get_file(client, "application_draft.pdf", version=0).status_code == 422
    assert client.get(f"{FILES}/application_draft.pdf", params={"version": -1}).status_code == 422
    bad_ver = client.get(f"{FILES}/application_draft.pdf", params={"version": "abc"})
    assert bad_ver.status_code == 422


def test_missing_cases_versions_and_unknown_or_traversal_names_are_404(
    fresh_db, client, other_org_client, session,
):
    # Case with no package built yet.
    assert get_file(client, "manifest.json").status_code == 404

    make_ready(client)
    upload_evidence(client, "leak.jpg", jpeg_with_gps(), "image/jpeg")
    built = build(client).json()
    jpg_name = next(
        f["filename"] for f in built["files"] if f["filename"].startswith("evidence/")
    )

    # Another org's case.
    assert get_file(other_org_client, "manifest.json").status_code == 404
    assert get_file(other_org_client, jpg_name, version=1).status_code == 404

    # Unknown version on the right case.
    assert get_file(client, "manifest.json", version=99).status_code == 404

    # Seeded evidence whose original was never stored is not in files.
    assert get_file(client, "evidence/E-01.pdf").status_code == 404

    # Names not in files: unknown, traversal (percent-encoded so httpx doesn't normalize it away),
    # and case mismatch.
    for raw_path in (
        "nope.pdf",
        "%2e%2e/manifest.json",
        "evidence/%2e%2e/manifest.json",
        jpg_name.upper(),
        "MANIFEST.JSON",
    ):
        r = client.get(f"{FILES}/{raw_path}")
        assert r.status_code == 404, f"{raw_path}: {r.status_code}"
        assert r.json()["detail"] == "File not found"

    # Also verify package_file itself rejects literal traversal names without path cleaning.
    row = package_rows(session)[0]
    for bad_name in ("../manifest.json", "evidence/../manifest.json", jpg_name.upper()):
        with pytest.raises(HTTPException) as exc:
            pkg_svc.package_file(row, bad_name)
        assert exc.value.status_code == 404
        assert exc.value.detail == "File not found"


def test_anonymous_cannot_download_package_file(fresh_db, client, anon_client):
    make_ready(client)
    build(client)
    assert get_file(anon_client, "manifest.json").status_code == 401
    assert get_file(anon_client, "manifest.json", version=1).status_code == 401


def test_response_headers_match_contract(fresh_db, client):
    make_ready(client)
    upload_evidence(client, "photo.jpg", jpeg_with_gps(), "image/jpeg")
    upload_evidence(client, "notes.txt", b"Line 1\n", "text/plain")
    built = build(client).json()

    expected_types = {
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".jpg": "image/jpeg",
        ".txt": "text/plain",
    }
    for entry in built["files"]:
        name = entry["filename"]
        r = get_file(client, name)
        assert r.status_code == 200, f"{name}: {r.text}"
        leaf = PurePosixPath(name).name
        assert "/" not in leaf
        assert r.headers["content-disposition"] == f'attachment; filename="{leaf}"'
        assert r.headers["cache-control"] == "private, no-store"
        assert r.headers["x-file-sha256"] == entry["sha256"]
        assert int(r.headers["content-length"]) == len(r.content)
        suffix = PurePosixPath(name).suffix
        assert r.headers["content-type"].startswith(expected_types[suffix])


def test_hash_mismatch_returns_500_and_logs_warning(fresh_db, client, monkeypatch, caplog):
    make_ready(client)
    build(client)

    real_get_package = pkg_svc.get_package

    def tampered_get_package(session, case, version):
        row = real_get_package(session, case, version)
        row.files = [
            {**item, "sha256": "0" * 64} if item["filename"] == "manifest.json" else item
            for item in row.files
        ]
        return row

    # Alembic's fileConfig disables loggers imported before the test DB is migrated.
    monkeypatch.setattr(pkg_svc.log, "disabled", False)
    monkeypatch.setattr(pkg_svc, "get_package", tampered_get_package)
    with caplog.at_level(logging.WARNING, logger="app.services.package"):
        r = get_file(client, "manifest.json")

    assert r.status_code == 500
    assert r.json()["detail"] == "Package file does not match its recorded hash"
    warnings = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any(
        CASE in msg
        and "1" in msg
        and "manifest.json" in msg
        and "caseworker_attestations" not in msg
        for msg in warnings
    ), warnings


def test_package_page_links_each_file_in_real_mode_and_uses_plain_text_in_mock():
    source = INDEX.read_text(encoding="utf-8")
    view = source[source.index("function viewPackage()"):source.index("function reqHtml(")]
    assert 'if (USE_MOCK) return esc(r.filename);' in view
    assert 'r.filename.split("/").map(encodeURIComponent).join("/")' in view
    assert (
        '<a href="${API_BASE}${C}/package/files/${path}?version=${S.pkg.version}" '
        'download>${esc(r.filename)}</a>'
    ) in view
    result_fn = source[
        source.index("function packageResult("):source.index("function viewPackage(")
    ]
    assert "upload the files inside one by one" not in result_fn
    assert "table below" in result_fn
