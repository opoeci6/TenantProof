"""Package gating and stage rules. Pure: no I/O, no LLM (CLAUDE.md)."""

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class OpenGap:
    title: str
    field_key: str | None


@dataclass(frozen=True)
class MissingField:
    key: str
    label: str


def package_blockers(
    open_blocking_gaps: Sequence[OpenGap],
    missing_required_fields: Sequence[MissingField],
    tenant_attested: bool,
) -> list[str]:
    """Reasons the package cannot be built yet. Empty list = ready.
    A missing required field already covered by an open blocking gap is not listed twice."""
    reasons = [f"Required item still open: {g.title}" for g in open_blocking_gaps]
    covered = {g.field_key for g in open_blocking_gaps if g.field_key}
    reasons += [
        f"Required field is empty: {f.label}"
        for f in missing_required_fields
        if f.key not in covered
    ]
    if not tenant_attested:
        reasons.append("The tenant hasn't confirmed their files are genuine")
    return reasons


def compute_stage(
    current: str, *, open_blocking: int, pending_review: int, waiting_on_tenant: bool
) -> str:
    """Stage after a change. `hearing_scheduled` is set elsewhere and always preserved."""
    if current == "hearing_scheduled":
        return current
    if waiting_on_tenant:
        return "waiting_tenant"
    if open_blocking == 0 and pending_review == 0:
        return "ready_to_file"
    return "needs_review"
