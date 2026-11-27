"""Issue #165: the privacy list costs nothing with PRIVACY_PLACEHOLDERS off, and the finder takes
time linear in the length of the text, finding the same email addresses as before."""

import random
import re
import time
from collections.abc import Callable, Iterable

import pytest
from sqlmodel import Session

from app.config import get_settings
from app.models import CaseRow, EvidencePageRow
from app.routers.privacy import privacy_view
from app.services.agent import placeholders
from app.services.agent.evidence_text import SqlTextStore
from app.services.personal_details import PATTERNS, Placeholders
from tests.agent_support import CASE

# --- GET /privacy


def _add_page(session: Session) -> None:
    session.add(EvidencePageRow(
        case_id=CASE, evidence_id="ev_04", position=0, locator="message 1",
        text="Jordan Lee, Unit 4B, 100 Example Street. Call 902-555-0187 or jo.lee@example.test",
    ))  # fmt: skip
    session.commit()


def test_with_the_setting_off_the_list_reads_no_evidence(
    fresh_db: None, client, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_page(session)
    monkeypatch.setattr(get_settings(), "privacy_placeholders", False)

    def no_scan(*_: object, **__: object) -> None:
        raise AssertionError("the privacy list was built with the setting off")

    monkeypatch.setattr(placeholders, "build", no_scan)
    monkeypatch.setattr(SqlTextStore, "pages", no_scan)
    monkeypatch.setattr(Placeholders, "hide", no_scan)
    r = client.get(f"/api/cases/{CASE}/privacy")
    assert r.status_code == 200, r.text
    assert r.json() == {"enabled": False, "details": []}


def test_with_the_setting_on_the_list_is_built_as_before(
    fresh_db: None, client, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _add_page(session)
    monkeypatch.setattr(get_settings(), "privacy_placeholders", True)
    r = client.get(f"/api/cases/{CASE}/privacy")
    assert r.status_code == 200, r.text
    case = session.get(CaseRow, CASE)
    assert case is not None
    assert r.json() == privacy_view(session, case).model_dump()
    found = {d["value"]: d["placeholder"] for d in r.json()["details"]}
    assert found["Jordan Lee"] == "[TENANT_NAME]"
    assert found["jo.lee@example.test"] == "[EMAIL_1]"


# --- EMAIL: the same matches as the pattern before #165

# The pattern as it was before #165: matches could start anywhere, so long runs were quadratic.
_OLD_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9])"
)
_EMAIL = next(p for p in PATTERNS if p.stem == "EMAIL")
_LONG_LOCAL = "a" * 64 + "@" + "d" * 60 + ".example.test"  # RFC 5321's longest local part
_VERY_LONG_LOCAL = "b" * 300 + "@example.test"  # longer than any real one: still found

EMAIL_CASES: list[tuple[str, list[str]]] = [
    ("Write to jordan.lee@example.test today.", ["jordan.lee@example.test"]),
    ("Mail Jordan.Lee+rent@Mail.Example.CA now", ["Jordan.Lee+rent@Mail.Example.CA"]),
    ("JORDAN@EXAMPLE.TEST", ["JORDAN@EXAMPLE.TEST"]),
    ("o'brien-smith_99%x@sub.dom-ain.example.org", ["brien-smith_99%x@sub.dom-ain.example.org"]),
    ("x@123.example.ca and x@my-host.example.ca", ["x@123.example.ca", "x@my-host.example.ca"]),
    ("<mei.chen@example.test>", ["mei.chen@example.test"]),
    ("(mei.chen@example.test)", ["mei.chen@example.test"]),
    ('"a@b.co", mailto:a@b.co; Email:jo@ex.com.', ["a@b.co", "a@b.co", "jo@ex.com"]),
    (_LONG_LOCAL, [_LONG_LOCAL]),
    (_VERY_LONG_LOCAL, [_VERY_LONG_LOCAL]),
    ("邮箱：mei.chen@example.test，谢谢", ["mei.chen@example.test"]),
    ("Écrivez à émile@exemple.test", ["mile@exemple.test"]),
    # An address straight after another one starts where the first ended.
    ("jo@ex.com-ann@ex.com", ["jo@ex.com", "-ann@ex.com"]),
    ("a@b.com.x@y.org", ["a@b.com", ".x@y.org"]),
    ("a@b.com_x@y.org", ["a@b.com", "_x@y.org"]),
    ("a@b.c@d.com", ["b.c@d.com"]),
    ("jo@ex.co.uk1 jo@ex.co.uk", ["jo@ex.co", "jo@ex.co.uk"]),
    # Not addresses.
    ("jo@ex jo@ex.c jo@.com @ex.com jo@ex.com1", []),
    ("user at example dot com, 2026-08-15@10:30, [EMAIL_1]", []),
    ("mei@例子.test jo@[192.168.0.1]", []),
]


def _spans(matches: Iterable[re.Match[str]]) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0)) for m in matches]


@pytest.mark.parametrize(("text", "expected"), EMAIL_CASES)
def test_email_matches_are_the_same_as_before(text: str, expected: list[str]) -> None:
    old = _spans(_OLD_EMAIL.finditer(text))
    assert [s[2] for s in old] == expected
    assert _spans(_EMAIL.finditer(text)) == old


def test_email_matches_are_the_same_as_before_on_random_text() -> None:
    rnd = random.Random(165)
    for _ in range(5_000):
        text = "".join(rnd.choice("ab.Z0-_+%@x é") for _ in range(rnd.randint(1, 40)))
        assert _spans(_EMAIL.finditer(text)) == _spans(_OLD_EMAIL.finditer(text)), text


# --- time grows linearly with the length of the text (#165)


def _noise(alphabet: str) -> Callable[[int], str]:
    def make(n: int) -> str:
        rnd = random.Random(n)
        return "".join(rnd.choice(alphabet) for _ in range(n))

    return make


LONG_TEXTS: dict[str, Callable[[int], str]] = {
    # base64 or OCR noise: letters, digits and dots without a space, an @ every 100k
    "no spaces": lambda n: "@".join(
        _noise("ABCXYZabcxyz0123456789.")(n)[i : i + 100_000] for i in range(0, n, 100_000)
    ),
    "digits and separators": _noise("0123456789.-+"),
    "long words after a number": lambda n: "12 " + " ".join(["Aaaaaaaaa" * 50] * (n // 451)),
}


def _seconds(text: str) -> float:
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        Placeholders().hide(text)
        best = min(best, time.perf_counter() - start)
    return best


@pytest.mark.parametrize("kind", LONG_TEXTS)
def test_long_unbroken_text_is_scanned_in_linear_time(kind: str) -> None:
    make = LONG_TEXTS[kind]
    big, half = _seconds(make(200_000)), _seconds(make(100_000))
    assert big < 1.0  # before #165, 200k characters without spaces took tens of seconds
    assert big < 3 * max(half, 0.005)  # quadratic would be 4x
