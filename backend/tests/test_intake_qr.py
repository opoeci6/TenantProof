"""#56: the QR code of a newly sent intake link. It comes only with the POST that issues the link
(like the link itself), encodes that link and nothing else, and never leaves another org."""

from urllib.parse import unquote

import segno

CASE = "case_demo_001"


def _send(client, case_id: str = CASE):
    return client.post(f"/api/cases/{case_id}/intake-link")


def test_the_post_returns_a_qr_code_of_the_new_link(fresh_db, client):
    r = _send(client)
    assert r.status_code == 200
    body = r.json()
    assert body["qr_svg"].startswith("data:image/svg+xml")
    # Made independently here: the same link at the same settings gives the same QR code, and
    # the matrix segno builds for that link is exactly what the SVG draws.
    expected = segno.make(body["url"], error="m")
    assert body["qr_svg"] == expected.svg_data_uri(
        scale=4, border=4, dark="#000", light="#fff"
    )
    assert segno.make(body["url"] + "x", error="m").svg_data_uri(
        scale=4, border=4, dark="#000", light="#fff"
    ) != body["qr_svg"]
    svg = unquote(body["qr_svg"].split(",", 1)[1])
    assert svg.startswith("<svg") and "<script" not in svg.lower()
    assert body["url"] not in svg  # the link is in the pattern only, not as text


def test_each_new_link_gets_its_own_qr_code(fresh_db, client):
    first, second = _send(client).json(), _send(client).json()
    assert first["url"] != second["url"]
    assert first["qr_svg"] != second["qr_svg"]


def test_no_get_returns_a_qr_code(fresh_db, client):
    assert _send(client).status_code == 200
    assert client.get(f"/api/cases/{CASE}").json()["intake"]["qr_svg"] is None
    assert "data:image/svg+xml" not in client.get("/api/cases").text


def test_another_orgs_case_gets_no_link_and_no_qr_code(fresh_db, other_org_client):
    r = _send(other_org_client)
    assert r.status_code == 404
    assert "qr_svg" not in r.text
