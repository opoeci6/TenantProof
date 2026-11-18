"""#58: rate limits count the visitor's own IP behind a trusted proxy, and only then.
#59: expired login sessions are cleared out on login and by the app's hourly task."""

import time
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.auth import _hash_cookie, make_session_for_tests, purge_expired_sessions
from app.config import Settings, get_settings
from app.main import app
from app.models import LoginSessionRow, UserRow
from tests.conftest import CASEWORKER, OTHER_CASEWORKER, PASSWORD

LOGIN = "/api/auth/login"


def _wrong_login(client: TestClient, ip: str):
    return client.post(
        LOGIN, json={"email": CASEWORKER, "password": "wrong"},
        headers={"X-Forwarded-For": ip},
    )


# --- #58 --------------------------------------------------------------------------------------


def test_the_app_believes_only_the_configured_proxies():
    wrapped = [m for m in app.user_middleware if m.cls is ProxyHeadersMiddleware]
    assert len(wrapped) == 1
    assert wrapped[0].kwargs == {"trusted_hosts": get_settings().forwarded_allow_ips}


def test_the_default_trusts_only_localhost(monkeypatch):
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    assert Settings(_env_file=None).forwarded_allow_ips == "127.0.0.1"  # type: ignore[call-arg]


def test_behind_a_trusted_proxy_each_visitor_has_its_own_login_budget(fresh_db):
    # The app as served: its own middleware, its default trust list. The client connects from
    # 127.0.0.1, so it plays a proxy on the same host. Without app.add_middleware(...) in
    # main.py both visitors would share 127.0.0.1's budget and the last call would be 429.
    assert get_settings().forwarded_allow_ips == "127.0.0.1"
    proxy = TestClient(app, base_url="https://testserver", client=("127.0.0.1", 50000))
    for _ in range(10):
        assert _wrong_login(proxy, "203.0.113.7").status_code == 401
    assert _wrong_login(proxy, "203.0.113.7").status_code == 429
    # Another visitor behind the same proxy is not locked out by the first one.
    assert _wrong_login(proxy, "198.51.100.20").status_code == 401


def test_a_forwarded_for_header_from_an_untrusted_sender_is_ignored(fresh_db, anon_client):
    # "testclient" is not in the default trusted list, so the header changes nothing and both
    # "visitors" share the sender's own budget.
    for _ in range(10):
        assert _wrong_login(anon_client, "203.0.113.7").status_code == 401
    assert _wrong_login(anon_client, "198.51.100.20").status_code == 429


# --- #59 --------------------------------------------------------------------------------------


def _user_id(session: Session, email: str) -> str:
    return session.exec(select(UserRow).where(UserRow.email == email)).one().id


def _add_session(session: Session, user_id: str, expires_at: datetime) -> str:
    token_hash = _hash_cookie(f"{user_id}-{expires_at.isoformat()}")
    session.add(LoginSessionRow(token_hash=token_hash, user_id=user_id, expires_at=expires_at))
    session.commit()
    return token_hash


def _left(session: Session, token_hashes: list[str]) -> set[str]:
    session.expire_all()
    rows = session.exec(
        select(LoginSessionRow).where(LoginSessionRow.token_hash.in_(token_hashes))  # type: ignore[attr-defined]
    ).all()
    return {r.token_hash for r in rows}


def test_a_login_clears_out_expired_sessions_and_keeps_live_ones(fresh_db, anon_client, session):
    now = datetime.now(UTC)
    me, other = _user_id(session, CASEWORKER), _user_id(session, OTHER_CASEWORKER)
    expired = [
        _add_session(session, me, now - timedelta(days=3)),
        _add_session(session, other, now - timedelta(seconds=1)),
    ]
    live = [
        _add_session(session, me, now + timedelta(hours=2)),
        _add_session(session, other, now + timedelta(hours=2)),
    ]
    other_cookie = make_session_for_tests(session, other)
    live.append(_hash_cookie(other_cookie))

    r = anon_client.post(LOGIN, json={"email": CASEWORKER, "password": PASSWORD})

    assert r.status_code == 200
    assert _left(session, expired + live) == set(live)


def test_a_failed_login_deletes_nothing(fresh_db, anon_client, session):
    expired = _add_session(
        session, _user_id(session, CASEWORKER), datetime.now(UTC) - timedelta(hours=1)
    )
    r = anon_client.post(LOGIN, json={"email": CASEWORKER, "password": "wrong"})
    assert r.status_code == 401
    assert _left(session, [expired]) == {expired}


def test_a_session_that_expires_later_is_cleared_out_only_once_it_has(fresh_db, session):
    now = datetime.now(UTC)
    soon = _add_session(session, _user_id(session, CASEWORKER), now + timedelta(seconds=30))

    assert purge_expired_sessions(session, now=now) == 0
    session.commit()
    assert _left(session, [soon]) == {soon}

    assert purge_expired_sessions(session, now=now + timedelta(seconds=31)) == 1
    session.commit()
    assert _left(session, [soon]) == set()


def test_the_app_task_clears_out_expired_sessions_and_stops_with_the_app(fresh_db, session):
    now = datetime.now(UTC)
    me = _user_id(session, CASEWORKER)
    expired = _add_session(session, me, now - timedelta(minutes=5))
    live = _add_session(session, me, now + timedelta(hours=1))

    with TestClient(app):  # runs the lifespan: the task purges at once, then hourly
        task = app.state.purge_task
        deadline = time.monotonic() + 10
        while expired in _left(session, [expired]) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _left(session, [expired, live]) == {live}
        assert not task.done()

    assert task.done()
