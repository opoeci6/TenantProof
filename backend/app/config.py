import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://tenantproof:tenantproof@localhost:5432/tenantproof"
    redis_url: str = "redis://localhost:6379/0"
    llm_mode: Literal["live", "fixture"] = "live"
    # Live model name. Empty means the built-in default (`llm.MODEL`, claude-sonnet-5-5).
    llm_model: str = ""
    # Base URL of an Anthropic-compatible API. Empty means Anthropic's own. The API key and all
    # evidence text sent to the model go to this host, so only https (or plain http to
    # localhost, for a local proxy) is accepted.
    anthropic_base_url: str = ""
    # Seconds one analysis run may take, all steps together. When it passes, the step in progress
    # ends with what it has and the later steps do not run. One model call stays capped at 60 s.
    run_budget_s: float = Field(default=180.0, ge=10, le=900)
    # Session cookie. Secure must stay true anywhere but plain-http localhost (compose sets false).
    cookie_secure: bool = True
    session_ttl_hours: int = 12
    # Password for the seeded caseworkers. Required to seed; never committed.
    seed_password: str | None = None
    # Rate-limit counters live in Redis so they hold across API replicas (tests and CI too).
    rate_limit_storage_uri: str | None = None
    login_rate_limit: str = "10/minute"
    submit_rate_limit: str = "20/minute"
    # Proxies whose X-Forwarded-For and X-Forwarded-Proto are believed (comma list of IPs or
    # CIDRs, or "*"), so the rate limits count each visitor's own IP. "*" is safe only when the
    # proxy alone can reach the API port. Same name as uvicorn's own setting (whose default also
    # trusts ::1). Behind a published Docker port the API sees the compose network's gateway,
    # never 127.0.0.1, so the default never trusts a proxy there.
    forwarded_allow_ips: str = "127.0.0.1"
    # Evidence uploads: per-file size cap, and a rate limit per client IP on both upload routes.
    max_upload_bytes: int = 25 * 1024 * 1024
    upload_rate_limit: str = "30/minute"
    # OCR on this server (RapidOCR, no network): text in photos and in scanned PDF pages becomes
    # evidence text the agent can cite. One upload request spends at most `ocr_request_seconds`
    # on it; later images are kept without their text, and the evidence says so.
    ocr_enabled: bool = True
    ocr_request_seconds: float = 40.0
    # Content-addressed blob store: package zips, original evidence files and stripped copies.
    storage_dir: str = "./data/storage"
    web_dir: str = "../web"
    public_base_url: str = "http://localhost:8000"
    intake_token_ttl_days: int = 14

    @field_validator("llm_model")
    @classmethod
    def _model_name(cls, value: str) -> str:
        value = value.strip()
        if value and not _MODEL_NAME.match(value):
            raise ValueError("LLM_MODEL must be a model name such as claude-sonnet-5-5")
        return value

    @field_validator("anthropic_base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return ""
        parts = urlsplit(value)
        secure = parts.scheme == "https" and bool(parts.hostname)
        local = parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS
        if not (secure or local) or parts.username or parts.password:
            raise ValueError(
                "ANTHROPIC_BASE_URL must be an https URL (or http://localhost) without a "
                "user name or password"
            )
        return value

    @property
    def limiter_uri(self) -> str:
        return self.rate_limit_storage_uri or self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
