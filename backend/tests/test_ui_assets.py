"""The UI is split into index.html, index.css and index.js. The API serves all three from the
site root, so the tenant link (/i/{token}) loads the same stylesheet and script as the
caseworker page; opened straight from disk, the page's relative <base> keeps mock mode working."""

from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"


def test_the_page_loads_its_assets_by_name(fresh_db, anon_client):
    page = anon_client.get("/")
    assert page.status_code == 200
    assert 'href="index.css"' in page.text
    assert 'src="index.js"' in page.text
    # no inline style or script left: both are external files now
    assert "<style" not in page.text
    assert "<script>" not in page.text


def test_the_stylesheet_is_served(fresh_db, anon_client):
    r = anon_client.get("/index.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert r.headers["cache-control"] == "no-cache"
    assert r.text == (WEB / "index.css").read_text(encoding="utf-8")


def test_the_script_is_served(fresh_db, anon_client):
    r = anon_client.get("/index.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["cache-control"] == "no-cache"
    assert r.text == (WEB / "index.js").read_text(encoding="utf-8")


def test_the_tenant_link_gets_a_root_base(fresh_db, anon_client):
    page = anon_client.get("/i/not-a-token")
    assert page.status_code == 200
    assert '<base href="/">' in page.text
    assert 'href="index.css"' in page.text
    # the caseworker page keeps the document URL as its base, so "#..." links keep ?case=
    assert "<base" not in anon_client.get("/").text
