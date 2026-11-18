"""Append-only activity log. Every mutation calls log() in the same transaction."""

from datetime import UTC, datetime

from sqlmodel import Session

from app.models import ActivityRow


def log(session: Session, case_id: str, actor: str, actor_name: str, action: str) -> None:
    session.add(
        ActivityRow(
            case_id=case_id, at=datetime.now(UTC), actor=actor, actor_name=actor_name,
            action=action,
        )
    )
