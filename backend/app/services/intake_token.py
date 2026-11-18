"""Intake link tokens. Random, shown once, stored only as sha256 + expiry + revoked flag."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.models import IntakeTokenRow


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(session: Session, case_id: str, ttl_days: int) -> str:
    """Revoke every earlier token for the case, store a new one's hash, return the raw token.
    Does not commit: the caller commits together with its activity entry."""
    for old in session.exec(
        select(IntakeTokenRow).where(IntakeTokenRow.case_id == case_id, ~IntakeTokenRow.revoked)  # type: ignore[arg-type]
    ).all():
        old.revoked = True
        session.add(old)
    token = new_token()
    session.add(
        IntakeTokenRow(
            token_hash=hash_token(token),
            case_id=case_id,
            expires_at=datetime.now(UTC) + timedelta(days=ttl_days),
        )
    )
    return token


def find_active(session: Session, token: str) -> IntakeTokenRow | None:
    """The token row if it exists, is not revoked and not expired; else None."""
    row = session.get(IntakeTokenRow, hash_token(token))
    if row is None or row.revoked:
        return None
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
    return None if expires < datetime.now(UTC) else row
