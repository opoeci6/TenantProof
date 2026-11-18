"""Readiness for `GET /healthz`: is the API useful right now? Both Postgres and Redis must answer
(WS2; from YCL0609's `m2-deploy` branch).

The endpoint is public and needs no login, so it stays cheap and says little:
- one check every `TTL_S` seconds at most; callers in between get the stored result, and a call
  that arrives while a check is running gets the previous result instead of waiting;
- the probes reuse the app's database pool and one Redis client, with short timeouts;
- which dependency failed goes to the log, once per change, never into the response.

A Postgres host that drops packets instead of refusing can still hold one check for the OS
connect timeout; the stored result keeps every other caller from waiting on it."""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache

import redis
from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine

log = logging.getLogger(__name__)
TTL_S = 5.0
REDIS_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class Health:
    db: bool
    redis: bool

    @property
    def ok(self) -> bool:
        return self.db and self.redis

    def describe(self) -> str:
        return f"db={'ok' if self.db else 'error'} redis={'ok' if self.redis else 'error'}"


def db_reachable() -> bool:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # any failure means "not reachable"; the type goes to the log
        log.debug("healthz: Postgres probe failed: %s", type(exc).__name__)
        return False


@cache
def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().limiter_uri,
        socket_connect_timeout=REDIS_TIMEOUT_S,
        socket_timeout=REDIS_TIMEOUT_S,
    )


def redis_reachable() -> bool:
    try:
        return bool(_redis_client().ping())
    except Exception as exc:  # as above
        log.debug("healthz: Redis probe failed: %s", type(exc).__name__)
        return False


class HealthCheck:
    """The stored result and when it was taken. One instance serves the app."""

    def __init__(
        self,
        db_probe: Callable[[], bool] = db_reachable,
        redis_probe: Callable[[], bool] = redis_reachable,
        clock: Callable[[], float] = time.monotonic,
        ttl_s: float = TTL_S,
    ) -> None:
        self._db_probe = db_probe
        self._redis_probe = redis_probe
        self._clock = clock
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._last: Health | None = None
        self._checked_at = 0.0

    def current(self) -> Health:
        last = self._last
        if last is not None and self._clock() - self._checked_at < self._ttl_s:
            return last
        # The first check ever waits; later ones never queue behind a slow check.
        if not self._lock.acquire(blocking=last is None):
            assert last is not None
            return last
        try:
            if self._last is not None and self._last is not last:
                return self._last  # another caller checked while this one waited
            fresh = Health(db=self._db_probe(), redis=self._redis_probe())
            self._report(last, fresh)
            self._last, self._checked_at = fresh, self._clock()
            return fresh
        finally:
            self._lock.release()

    @staticmethod
    def _report(before: Health | None, after: Health) -> None:
        if before == after or (before is None and after.ok):
            return
        # Both at WARNING: the API sets no logging config, so only WARNING and up reach its output.
        state = "recovered" if after.ok else "degraded"
        log.warning("healthz: %s (%s)", state, after.describe())


checker = HealthCheck()
