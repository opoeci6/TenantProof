"""Caseworker login: httpOnly session cookie, server-side sessions, hashed cookie values,
login rate limit, Secure flag, and the Origin check on mutations
(CONTRACT_CHANGES #10, BUILD_PLAN section 7)."""

import hashlib
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import select

from app.config import Settings, get_settings
from app.main import api, app
from app.models import LoginSessionRow, OrgRow, UserRow
from tests.conftest import CASEWORKER, OTHER_CASEWORKER, PASSWORD

CASE = "case_demo_001"
LOGIN = "/api/auth/login"


def _login(client: TestClient, email: str = CASEWORKER, password: str = PASSWORD):
    return client.post(LOGIN, json={"email": email, "password": password})


def _set_cookie(response) -> str:
    """The tp_session Set-Cookie header of a response."""
    headers = [h for h in response.headers.get_list("set-cookie") if h.startswith("tp_session=")]
    assert len(headers) == 1, response.headers.get_list("set-cookie")
    return headers[0]


def _value(set_cookie: str) -> str:
    return set_cookie.split(";", 1)[0].split("=", 1)[1]


def _attrs(set_cookie: str) -> set[str]:
    """Cookie attributes as lowercase tokens, so 'Secure' and 'secure' compare equal."""
    return {part.strip().lower() for part in set_cookie.split(";")[1:]}


def _raw_client(cookie: str | None) -> TestClient:
    """A brand-new client, so only the cookie we send by hand is in play."""
    c = TestClient(app, base_url="https://testserver")
    if cookie is not None:
        c.headers["cookie"] = f"tp_session={cookie}"
    return c


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_login_success_returns_user_and_secure_cookie(fresh_db, anon_client, session):
    r = _login(anon_client)
    assert r.status_code == 200
    body = r.json()
    user = session.exec(select(UserRow).where(UserRow.email == CASEWORKER)).one()
    org = session.get(OrgRow, user.org_id)
    assert org is not None
    assert body["role"] == "caseworker"
    assert body["org"] == {"id": org.id, "name": org.name}
    assert set(body) == {"id", "name", "role", "org"}

    cookie = _set_cookie(r)
    assert _value(cookie)
    attrs = _attrs(cookie)
    assert "httponly" in attrs
    assert "secure" in attrs
    assert "samesite=lax" in attrs
    assert "path=/" in attrs


def test_other_org_login_gets_its_own_org(fresh_db, anon_client, session):
    r = _login(anon_client, email=OTHER_CASEWORKER)
    assert r.status_code == 200
    other_org = session.exec(select(UserRow).where(UserRow.email == OTHER_CASEWORKER)).one()
    org = session.get(OrgRow, other_org.org_id)
    assert org is not None
    assert r.json()["org"] == {"id": org.id, "name": org.name}
    assert anon_client.get("/api/cases/case_demo_001").status_code == 404


def test_cookie_from_login_authenticates_later_requests(fresh_db, anon_client):
    assert _login(anon_client).status_code == 200
    me = anon_client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["role"] == "caseworker"


def test_wrong_password_and_unknown_email_give_identical_401(fresh_db, anon_client):
    wrong_pw = _login(anon_client, password="not-the-password")
    unknown = _login(anon_client, email="nobody@example.test")
    assert wrong_pw.status_code == 401
    assert unknown.status_code == 401
    assert wrong_pw.json() == unknown.json()
    assert "set-cookie" not in wrong_pw.headers
    assert "set-cookie" not in unknown.headers





def test_unknown_cookie_value_is_401(anon_client):
    assert _raw_client("not-a-real-session").get("/api/cases").status_code == 401


def test_tampered_real_cookie_is_401(fresh_db, anon_client):
    value = _value(_set_cookie(_login(anon_client)))
    flipped = value[:-1] + ("A" if value[-1] != "A" else "B")
    assert flipped != value
    assert _raw_client(flipped).get("/api/cases").status_code == 401
    assert _raw_client(value).get("/api/cases").status_code == 200  # control: original works


def test_logout_returns_ok_clears_cookie_and_kills_the_session(fresh_db, anon_client, session):
    login = _login(anon_client)
    value = _value(_set_cookie(login))
    assert anon_client.cookies.get("tp_session") == value

    out = anon_client.post("/api/auth/logout")
    assert out.status_code == 200
    assert out.json() == {"ok": True}
    cleared = _set_cookie(out)
    assert _attrs(cleared) >= {"max-age=0", "httponly"}

    # The old value no longer works, even when sent by hand on a new client.
    assert _raw_client(value).get("/api/cases").status_code == 401
    session.expire_all()
    assert session.get(LoginSessionRow, _sha256(value)) is None


def test_database_stores_only_the_cookie_hash(fresh_db, anon_client, session):
    value = _value(_set_cookie(_login(anon_client)))
    session.expire_all()
    rows = session.exec(select(LoginSessionRow)).all()
    hashes = {row.token_hash for row in rows}
    assert value not in hashes
    assert _sha256(value) in hashes
    dumped = " ".join(str(row.model_dump()) for row in rows)
    assert value not in dumped


def test_expired_session_is_401(fresh_db, anon_client, session):
    value = _value(_set_cookie(_login(anon_client)))
    assert anon_client.get("/api/me").status_code == 200
    session.expire_all()
    row = session.get(LoginSessionRow, _sha256(value))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    session.add(row)
    session.commit()
    assert anon_client.get("/api/me").status_code == 401


def test_login_is_rate_limited_to_ten_per_minute(fresh_db, anon_client):
    for _ in range(10):
        assert _login(anon_client, password="wrong").status_code == 401
    eleventh = _login(anon_client, password="wrong")
    assert eleventh.status_code == 429
    # Still limited for the rest of the window, even with the right password.
    assert _login(anon_client).status_code == 429


def test_cookie_secure_can_be_turned_off_for_plain_http_dev(fresh_db, anon_client):
    api.dependency_overrides[get_settings] = lambda: get_settings().model_copy(
        update={"cookie_secure": False}
    )
    try:
        r = _login(anon_client)
    finally:
        api.dependency_overrides.pop(get_settings, None)
    assert r.status_code == 200
    attrs = _attrs(_set_cookie(r))
    assert "secure" not in attrs
    assert "httponly" in attrs


def test_settings_ignore_a_developers_env_file(tmp_path, monkeypatch):
    """The quickstart's backend/.env sets COOKIE_SECURE=false; tests must not pick it up."""
    (tmp_path / ".env").write_text("COOKIE_SECURE=false\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert Settings().cookie_secure is True


def test_cross_origin_mutation_is_refused(fresh_db, client):
    before = len(client.get(f"/api/cases/{CASE}/activity").json())
    r = client.post(f"/api/cases/{CASE}/intake-link", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert r.json() == {"detail": "Cross-origin request refused"}
    assert len(client.get(f"/api/cases/{CASE}/activity").json()) == before


def test_same_origin_mutation_is_allowed(fresh_db, client):
    r = client.post(f"/api/cases/{CASE}/intake-link", headers={"Origin": "https://testserver"})
    assert r.status_code == 200
    assert r.json()["url"]
