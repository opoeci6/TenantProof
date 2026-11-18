"""Caseload priority rules (BUILD_PLAN section 6). Pure: no I/O, no LLM."""

from dataclasses import dataclass
from datetime import date

from app.services.legal.corpus import EVIDENCE_DAYS_BEFORE_HEARING

EVIDENCE_WINDOW_DAYS = 7


@dataclass(frozen=True)
class PriorityInput:
    stage: str
    next_deadline_date: date | None
    open_blocking: int
    pending_review: int
    # False only while the intake link has never gone out (a case just created, #41).
    intake_sent: bool = True


@dataclass(frozen=True)
class PriorityResult:
    priority: str
    reason: str
    days_to_deadline: int | None


def days_until(deadline: date | None, today: date) -> int | None:
    return None if deadline is None else (deadline - today).days


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def deadline_phrase(days: int) -> str:
    """How the caseload says when evidence must be received: today, in N days, or passed."""
    if days < 0:
        return f"Deadline passed ({_plural(-days, 'day')} ago)"
    if days == 0:
        return "Must be received today"
    return f"Must be received in {_plural(days, 'day')}"


def compute_priority(case: PriorityInput, today: date) -> PriorityResult:
    days = days_until(case.next_deadline_date, today)
    priority = "normal"
    reason: str | None = None

    if days is not None and days <= EVIDENCE_WINDOW_DAYS:
        priority = "high"
        reason = deadline_phrase(days)
        if days >= 0:
            reason += f" ({EVIDENCE_DAYS_BEFORE_HEARING} days before hearing)"
    if case.open_blocking and case.stage == "hearing_scheduled":
        priority = "high"
        blocked = f"{_plural(case.open_blocking, 'required item')} open with a hearing scheduled"
        reason = f"{reason} and {blocked}" if reason else blocked

    if reason is None:
        if case.stage == "waiting_tenant" and not case.intake_sent:
            reason = "Intake link not sent yet."
        elif case.stage == "waiting_tenant":
            reason = "Intake link sent. No files from the tenant yet."
        elif days is not None:
            reason = deadline_phrase(days)
        elif case.open_blocking or case.pending_review:
            reason = (
                f"{_plural(case.pending_review, 'event')} to review · "
                f"{_plural(case.open_blocking, 'required item')} open"
            )
        else:
            reason = "No open items"
    return PriorityResult(priority, reason, days)


def sort_key(priority: str, days: int | None, open_blocking: int, pending_review: int):
    """High first, then nearest deadline, then most blocking, then most pending."""
    return (0 if priority == "high" else 1, days if days is not None else 999,
            -open_blocking, -pending_review)
