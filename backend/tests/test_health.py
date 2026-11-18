"""`GET /healthz` is a readiness check: 503 when Postgres or Redis is down (WS2, from YCL0609's
`m2-deploy` branch). It is public, so it checks at most every few seconds, never queues callers
behind a slow check, logs which dependency failed once per change, and never says which in the
response."""

import logging
import threading

import pytest

from app.services import health
from app.services.health import Health, HealthCheck


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Probe:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.result


def _checker(db: Probe, rd: Probe, clock: Clock) -> HealthCheck:
    return HealthCheck(db_probe=db, redis_probe=rd, clock=clock, ttl_s=5.0)


@pytest.fixture
def use_checker(monkeypatch):
    def install(db: bool, rd: bool) -> None:
        monkeypatch.setattr(health, "checker", _checker(Probe(db), Probe(rd), Clock()))

    return install


def test_real_probes_see_the_test_postgres_and_redis() -> None:
    assert HealthCheck().current() == Health(db=True, redis=True)


def test_healthz_is_ok_when_both_answer(anon_client, use_checker) -> None:
    use_checker(db=True, rd=True)
    r = anon_client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "llm_mode": "fixture"}


@pytest.mark.parametrize(("db", "rd"), [(False, True), (True, False), (False, False)])
def test_healthz_is_503_and_says_nothing_about_which(anon_client, use_checker, db, rd) -> None:
    use_checker(db=db, rd=rd)
    r = anon_client.get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded", "llm_mode": "fixture"}


def test_result_is_reused_within_the_ttl() -> None:
    db, rd, clock = Probe(), Probe(), Clock()
    check = _checker(db, rd, clock)
    for _ in range(50):
        check.current()
    assert (db.calls, rd.calls) == (1, 1)
    clock.now += 4.9
    check.current()
    assert db.calls == 1
    clock.now += 0.2
    check.current()
    assert db.calls == 2


def test_a_change_is_seen_after_the_ttl() -> None:
    db, rd, clock = Probe(), Probe(), Clock()
    check = _checker(db, rd, clock)
    assert check.current().ok
    rd.result = False
    assert check.current().ok  # still the stored result
    clock.now += 5
    assert check.current() == Health(db=True, redis=False)


def test_callers_do_not_wait_behind_a_running_check() -> None:
    """While one caller's check is slow, another gets the previous result at once."""
    clock = Clock()
    started, release = threading.Event(), threading.Event()
    slow = {"on": False}

    def db_probe() -> bool:
        if slow["on"]:
            started.set()
            release.wait(5)
        return True

    check = HealthCheck(db_probe=db_probe, redis_probe=lambda: False, clock=clock, ttl_s=5.0)
    first = check.current()
    clock.now += 10
    slow["on"] = True
    worker = threading.Thread(target=check.current)
    worker.start()
    assert started.wait(5)
    assert check.current() is first  # did not block on the running check
    release.set()
    worker.join(5)
    assert not worker.is_alive()


def test_logs_once_per_change_with_the_failing_dependency(caplog, monkeypatch) -> None:
    db, rd, clock = Probe(), Probe(), Clock()
    check = _checker(db, rd, clock)
    # The test database is built with Alembic, whose fileConfig disables loggers that already
    # exist. Only the tests see that: the API process never runs Alembic's logging setup.
    monkeypatch.setattr(health.log, "disabled", False)
    caplog.set_level(logging.INFO, logger="app.services.health")

    check.current()  # healthy at start: nothing to say
    assert caplog.records == []

    rd.result = False
    for _ in range(3):
        clock.now += 5
        check.current()
    assert [r.getMessage() for r in caplog.records] == ["healthz: degraded (db=ok redis=error)"]
    assert caplog.records[0].levelno == logging.WARNING
    assert caplog.records[0].exc_info is None

    rd.result = True
    clock.now += 5
    check.current()
    assert caplog.records[-1].getMessage() == "healthz: recovered (db=ok redis=ok)"
    assert len(caplog.records) == 2


def test_a_failed_probe_is_false_not_an_error(monkeypatch) -> None:
    def boom() -> None:
        raise ConnectionError("no route")

    monkeypatch.setattr(health, "get_engine", boom)
    assert health.db_reachable() is False
