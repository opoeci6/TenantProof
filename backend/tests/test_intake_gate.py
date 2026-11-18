"""Issue #10: an upload with a bad intake token, or a caseworker upload without a login, is
refused before its body is read."""

import asyncio

from app.services import intake_gate
from app.services.intake_gate import IntakeTokenGate


def _run(
    path: str = "/api/intake/tok/files", **extra: object
) -> tuple[list[dict], int, bool]:
    """Run the gate with a body of one chunk. Returns messages sent, chunks read, app ran."""
    state = {"read": 0, "app": False}
    sent: list[dict] = []

    async def receive():
        state["read"] += 1
        return {"type": "http.request", "body": b"x" * 10, "more_body": True}

    async def send(message):
        sent.append(message)

    async def app(scope, receive, send):
        state["app"] = True
        message = await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": message.get("body", b"")})

    scope = {"type": "http", "method": "POST", "path": path, "headers": [], **extra}
    asyncio.run(IntakeTokenGate(app)(scope, receive, send))
    return sent, state["read"], state["app"]


def test_a_bad_token_is_404_without_reading_the_body(monkeypatch):
    monkeypatch.setattr(intake_gate, "token_is_active", lambda token: False)

    sent, read, ran = _run()

    assert read == 0 and not ran  # nothing of a 501 MB body touches this replica
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 404
    assert b"Link not found" in sent[1]["body"]


def test_a_good_token_reaches_the_app(monkeypatch):
    monkeypatch.setattr(intake_gate, "token_is_active", lambda token: True)

    sent, read, ran = _run()

    assert ran and read == 1
    assert sent[0]["status"] == 200


def test_a_signed_in_caseworker_upload_reaches_the_app(monkeypatch):
    checked: list[str] = []
    cookies: list[str | None] = []

    def record(token: str) -> bool:
        checked.append(token)
        return False

    def session_ok(cookie: str | None) -> bool:
        cookies.append(cookie)
        return True

    monkeypatch.setattr(intake_gate, "token_is_active", record)
    monkeypatch.setattr(intake_gate, "signed_in", session_ok)

    sent, _, ran = _run("/api/cases/case_demo_001/evidence",
                        headers=[(b"cookie", b"other=1; tp_session=abc")])

    assert ran and checked == [] and cookies == ["abc"]  # the intake check never runs here
    assert sent[0]["status"] == 200


def test_a_caseworker_upload_without_a_login_is_401_without_reading_the_body(monkeypatch):
    monkeypatch.setattr(intake_gate, "signed_in", lambda cookie: False)

    sent, read, ran = _run("/api/cases/case_demo_001/evidence")

    assert read == 0 and not ran
    assert sent[0]["status"] == 401 and b"Not signed in" in sent[1]["body"]


def test_the_root_path_is_stripped_like_the_router_does(monkeypatch):
    """uvicorn's --root-path puts the prefix in front of `path`; Starlette routes without it."""
    monkeypatch.setattr(intake_gate, "token_is_active", lambda token: False)

    sent, read, ran = _run("/tp/api/intake/tok/files", root_path="/tp")

    assert read == 0 and not ran and sent[0]["status"] == 404


def test_other_paths_and_methods_are_not_gated(monkeypatch):
    def boom(_: object) -> bool:
        raise AssertionError("no check expected")

    monkeypatch.setattr(intake_gate, "token_is_active", boom)
    monkeypatch.setattr(intake_gate, "signed_in", boom)
    for path in ("/api/intake/tok", "/api/intake/tok/submit", "/api/cases/c1/evidence/e1"):
        sent, _, ran = _run(path)
        assert ran and sent[0]["status"] == 200, path
    for method in ("GET", "PUT", "HEAD"):  # the gated paths, with methods that aren't uploads
        for path in ("/api/intake/tok/files", "/api/cases/c1/evidence"):
            sent, _, ran = _run(path, method=method)
            assert ran and sent[0]["status"] == 200, (method, path)
