"""Every answer carries nosniff, X-Frame-Options DENY and a strict Referrer-Policy; there is no
Content-Security-Policy (the single-page UI relies on inline script and style)."""

import pytest

from app.main import SECURITY_HEADERS

EXPECTED = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
}


def test_the_expected_headers_are_the_configured_ones():
    assert {k.lower(): v for k, v in SECURITY_HEADERS.items()} == EXPECTED


@pytest.mark.parametrize("path", ["/", "/healthz", "/favicon.ico", "/i/not-a-token"])
def test_the_ui_and_health_answers_carry_them(fresh_db, anon_client, path):
    r = anon_client.get(path)
    for name, value in EXPECTED.items():
        assert r.headers.get(name) == value, (path, name)
    assert "content-security-policy" not in r.headers


@pytest.mark.parametrize(("method", "path", "status"), [
    ("get", "/api/me", 401),  # an error answer
    ("get", "/api/cases/case_demo_001", 401),
    ("get", "/api/intake/not-a-token", 404),
    ("post", "/api/cases", 401),
])
def test_api_answers_carry_them_including_errors(fresh_db, anon_client, method, path, status):
    r = getattr(anon_client, method)(path)
    assert r.status_code == status
    for name, value in EXPECTED.items():
        assert r.headers.get(name) == value, (path, name)
    assert "content-security-policy" not in r.headers


def test_a_package_download_carries_them(fresh_db, client):
    r = client.get("/api/cases/case_demo_001/case-brief")
    assert r.status_code in (200, 409)
    for name, value in EXPECTED.items():
        assert r.headers.get(name) == value


def test_the_ui_loads_nothing_from_a_font_host():
    """No request to Google Fonts: it would tell Google every tenant's address, and the page
    would look different offline. The UI uses the system font stack."""
    from pathlib import Path

    page = (Path(__file__).resolve().parents[2] / "web" / "index.html").read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in page and "fonts.gstatic.com" not in page
    assert "system-ui" in page
