"""Rate limits (slowapi). Counters in Redis so they hold across API replicas."""

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings

limiter = Limiter(key_func=get_remote_address, storage_uri=get_settings().limiter_uri)


def login_limit() -> str:
    return get_settings().login_rate_limit


def submit_limit() -> str:
    return get_settings().submit_rate_limit
