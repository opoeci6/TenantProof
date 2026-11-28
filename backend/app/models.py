# pyright: reportAssignmentType=false
# (SQLModel declares __tablename__ as declared_attr; plain strings are the documented usage.)
"""SQLModel tables. Nested contract shapes are stored as JSON columns; the
routers validate them through the Pydantic models in schemas.py."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, CheckConstraint, Column, Index, String, UniqueConstraint, text
from sqlmodel import Field, SQLModel

from app.schemas import ApplicantRole, PrivacyAction, PrivacyRole


def utcnow() -> datetime:
    return datetime.now(UTC)


class OrgRow(SQLModel, table=True):
    __tablename__ = "org"
    id: str = Field(primary_key=True)
    name: str


class UserRow(SQLModel, table=True):
    __tablename__ = "app_user"
    id: str = Field(primary_key=True)
    org_id: str = Field(foreign_key="org.id", index=True)
    name: str
    email: str = Field(unique=True, index=True)
    role: str = "caseworker"
    password_hash: str | None = None  # argon2


class LoginSessionRow(SQLModel, table=True):
    """Server-side login session. Only sha256(cookie value) is stored."""

    __tablename__ = "login_session"
    token_hash: str = Field(primary_key=True)
    user_id: str = Field(foreign_key="app_user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime


class CaseRow(SQLModel, table=True):
    __tablename__ = "tenant_case"
    __table_args__ = (
        CheckConstraint(
            "applicant_role IN ('tenant', 'landlord')", name="ck_tenant_case_applicant_role"
        ),
        # #202: a retried create with the same key returns the case the first attempt made. The
        # key is per org, and rows without one (seeds, tests, older cases) never collide.
        Index(
            "uq_tenant_case_org_idempotency_key",
            "org_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )
    id: str = Field(primary_key=True)
    org_id: str = Field(foreign_key="org.id", index=True)
    assigned_to_id: str = Field(foreign_key="app_user.id")
    dispute_type: str = "maintenance"
    title: str
    unit: str
    tenant_name: str
    tenant_language: str
    applicant_role: ApplicantRole = Field(
        default="tenant", sa_column=Column(String(8), nullable=False, server_default="tenant")
    )
    stage: str
    hearing_date: str | None = None  # ISO date; drives deadline + priority
    no_hearing_yet: bool = False  # a caseworker said there is no hearing scheduled yet (#124)
    next_deadline: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    synthetic: bool = True
    has_demo_data: bool = False
    form_name: str = ""
    form_disclaimer: str = ""
    intake_status: str = "not_sent"
    intake_sent_at: datetime | None = None
    intake_last_upload_at: datetime | None = None
    tenant_attested_genuine: bool = False
    tenant_attested_at: datetime | None = None
    # #202: the key the new-case form minted for this submission; a retry with the same key
    # returns this case instead of creating a second one. Null for seeds and older cases.
    idempotency_key: str | None = Field(
        default=None, sa_column=Column(String(64), nullable=True)
    )
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class IntakeTokenRow(SQLModel, table=True):
    """Only sha256(token) is stored; the raw token is shown once, in the POST response.
    Re-sending the link revokes every earlier token for the case."""

    __tablename__ = "intake_token"
    token_hash: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    revoked: bool = False


class EvidenceRow(SQLModel, table=True):
    """`sha256` is of the original bytes and is also its storage key (chain of custody).
    The same file uploaded twice to one case is one row."""

    __tablename__ = "evidence"
    __table_args__ = (UniqueConstraint("case_id", "sha256"),)
    id: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True)
    position: int = 0
    label: str
    filename: str
    kind: str
    mime: str
    size_bytes: int
    sha256: str
    received_at: datetime
    uploaded_by: str
    format_check: dict[str, Any] = Field(sa_column=Column(JSON))
    metadata_: dict[str, Any] = Field(sa_column=Column("metadata", JSON))
    metadata_stripped_for_ai: bool = False
    processing: str = "done"
    error: str | None = None
    # Storage key of the copy with EXIF/GPS removed (images only). Only this copy may reach
    # the model; null when there is nothing to strip or stripping failed.
    stripped_sha256: str | None = None


class EvidencePageRow(SQLModel, table=True):
    """Stripped text of one evidence item, one row per locator ("page 1", "message 3"): what the
    agent's read_evidence serves and the verifier matches excerpts against. Written by ingest
    (PDF text per page, message lines), never from unstripped files. Deleting the evidence or
    the case deletes its pages."""

    __tablename__ = "evidence_page"
    evidence_id: str = Field(foreign_key="evidence.id", primary_key=True, ondelete="CASCADE")
    locator: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True, ondelete="CASCADE")
    position: int = 0
    text: str


class EvidenceFactRow(SQLModel, table=True):
    """One fact the extract step pulled from an evidence item, with its locator. Cached by
    (sha256 of the original bytes, prompt_version): unchanged evidence is not extracted twice."""

    __tablename__ = "evidence_fact"
    __table_args__ = (Index("ix_evidence_fact_cache", "sha256", "prompt_version"),)
    id: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True, ondelete="CASCADE")
    evidence_id: str = Field(foreign_key="evidence.id", index=True, ondelete="CASCADE")
    sha256: str
    prompt_version: str
    locator: str
    text: str
    position: int = 0


class RunRow(SQLModel, table=True):
    __tablename__ = "run"
    id: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True)
    status: str
    started_at: datetime
    finished_at: datetime | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    stale: bool = False
    usage: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))


class TimelineEventRow(SQLModel, table=True):
    __tablename__ = "timeline_event"
    id: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True)
    date: dict[str, Any] = Field(sa_column=Column(JSON))
    sort_key: str = ""  # derived from date.value; "9" sorts unknown dates last
    title: str
    description: str
    actor: str
    sources: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    strength: str
    strength_reasons: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    flags: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    review_status: str = "pending"
    caseworker_note: str | None = None
    reviewed_by: str | None = None


class GapRow(SQLModel, table=True):
    __tablename__ = "gap"
    id: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True)
    position: int = 0
    severity: str
    title: str
    why: str
    how_to_fix: str
    source_id: str | None = None
    input: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    tenant_request: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    # The tenant's own short answer (issue #95): { text, received_at, evidence_id }. It is also
    # an evidence item, so the agent cites it like a message; this copy only drives the card.
    answer: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    status: str = "open"
    resolution_note: str | None = None


class ExcerptTranslationRow(SQLModel, table=True):
    """One cached machine translation of a quoted source excerpt (issue #96). `key` is the
    sha256 of the language and the excerpt text, so the same quote is paid for once. A
    translation is a reading aid: it is never cited, filed or used as a quote check."""

    __tablename__ = "excerpt_translation"
    key: str = Field(primary_key=True)
    language: str
    text_en: str
    created_at: datetime = Field(default_factory=utcnow)


class LegalSourceRow(SQLModel, table=True):
    __tablename__ = "legal_source"
    id: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True)
    position: int = 0
    title: str
    kind: str
    url: str | None = None
    last_verified_at: datetime | None = None
    verify_status: str = "unverified"
    used_for: str = ""


class FormFieldRow(SQLModel, table=True):
    __tablename__ = "form_field"
    case_id: str = Field(foreign_key="tenant_case.id", primary_key=True)
    key: str = Field(primary_key=True)
    position: int = 0
    label: str
    value: str | None = None
    required: bool = True
    status: str = "missing"
    sources: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    note: str | None = None


class PrivacyTermRow(SQLModel, table=True):
    """A caseworker's choice about one personal detail of a case (issue #60). `hide` adds a
    value the finder missed (a name written in another script, a neighbour), so it is swapped
    for a placeholder before anything goes to the model. `show` lets a value the finder found
    go to the model as written. One row per value (compared case-insensitively, `value_key`)."""

    __tablename__ = "privacy_term"
    __table_args__ = (
        UniqueConstraint("case_id", "value_key"),
        CheckConstraint("action IN ('hide', 'show')", name="ck_privacy_term_action"),
        CheckConstraint(
            "role IN ('tenant', 'landlord', 'person', 'address', 'other')",
            name="ck_privacy_term_role",
        ),
    )
    id: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True, ondelete="CASCADE")
    value: str
    value_key: str  # the value casefolded with its spaces collapsed
    action: PrivacyAction = Field(sa_column=Column(String(4), nullable=False))
    role: PrivacyRole = Field(sa_column=Column(String(8), nullable=False))
    created_by: str  # the caseworker's display name, as in the activity log
    created_at: datetime = Field(default_factory=utcnow)


class ActivityRow(SQLModel, table=True):
    __tablename__ = "activity"
    id: int | None = Field(default=None, primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True)
    at: datetime
    actor: str
    actor_name: str
    action: str


class PackageRow(SQLModel, table=True):
    """One built filing package. Every build is a new version; rows are never overwritten.
    The zip itself lives in storage (content-addressed by sha256), not in the database."""

    __tablename__ = "package"
    __table_args__ = (UniqueConstraint("case_id", "version"),)
    id: str = Field(primary_key=True)
    case_id: str = Field(foreign_key="tenant_case.id", index=True)
    version: int
    sha256: str
    storage_key: str
    size_bytes: int
    prepared_by: str
    built_at: datetime = Field(default_factory=utcnow)
    attestations: dict[str, Any] = Field(sa_column=Column(JSON))
    files: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
