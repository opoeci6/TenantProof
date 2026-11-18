"""When evidence counts as received, as the app tells the caseworker (Regulations s. 25A(3B),
(3C); Evidence Policy #42, page 3). Only registered mail, express post or courier gets the
3rd-day rule, and e-mail sent *at or after* 4 p.m. counts the next day (leozhang5867's check on
issue #54). Ordinary mail has no received-by rule, so no text may say "mail" alone."""

import re
from pathlib import Path

import pytest

from app.services import package
from app.services.legal import corpus

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
SERVICES = "registered mail, express post or courier"
# "E-mail counts ..." is fine; "Mail counts ..." and "Mailed evidence counts ..." are not.
LOOSE_MAIL = re.compile(r"(?<![-\w])mail(?:ed)? (?:evidence )?counts\b", re.IGNORECASE)
LOOSE_4PM = re.compile(r"(?<!at or )after 4 p\.m\.")


def _mock_reminders() -> str:
    match = re.search(r"reminders: \[(.*?)\] \}", INDEX.read_text(encoding="utf-8"))
    assert match, "mock package reminders not found in web/index.html"
    return match.group(1)


def _texts() -> dict[str, str]:
    deadline_sources = {
        e.id: e.used_for
        for e in corpus.CORPUS
        if e.id in ("ns-rtr-25a", "ns-evidence-policy-42-deadline")
    }
    assert len(deadline_sources) == 2
    return {
        "package reminders": " ".join(package.REMINDERS),
        "mock reminders": _mock_reminders(),
        **deadline_sources,
    }


@pytest.mark.parametrize("name", list(_texts()))
def test_only_the_three_mail_services_get_the_third_day_rule(name: str) -> None:
    text = _texts()[name]
    assert SERVICES in text.lower()
    assert "3rd day after" in text
    assert not LOOSE_MAIL.search(text), text


@pytest.mark.parametrize("name", ["package reminders", "mock reminders", "ns-rtr-25a"])
def test_email_at_or_after_4pm_counts_the_next_day(name: str) -> None:
    text = _texts()[name]
    assert "at or after 4 p.m." in text
    assert "before 4 p.m." in text
    assert not LOOSE_4PM.search(text), text


def test_the_loose_patterns_catch_the_old_wording() -> None:
    old = (
        "Mailed evidence counts as received 3 days after it is sent, and e-mail sent after "
        "4 p.m. counts as received the next day."
    )
    assert LOOSE_MAIL.search(old)
    assert LOOSE_4PM.search(old)
    assert LOOSE_MAIL.search("Mail counts as received 3 days after sending")
    assert not LOOSE_MAIL.search("E-mail counts as received the same day")
