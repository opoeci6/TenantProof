"""Which intake link the caseworker pages show (`sessionLink` in web/index.html, a pure function
run under node). The raw link is returned only once, by `POST /cases/{id}/intake-link`, so the page
keeps it in memory with the case it was made for. Issue #38: the Tenant view shows it right after
"Send intake link", for that case only; another case, or a page with no link sent, shows none."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
BLOCK = re.compile(r"/\* Fix #38: intakeLink:start.*?\*/(.*?)/\* intakeLink:end \*/", re.S)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

# Placeholder, not a real token: tests never hold a real intake link.
LINK = "https://tenantproof.example/i/not-a-real-token"


def shown(*calls: tuple[object, object], fn: str = "sessionLink") -> list[object]:
    match = BLOCK.search(INDEX.read_text(encoding="utf-8"))
    assert match, "intakeLink block not found in web/index.html"
    args = ", ".join(f"[{json.dumps(last)}, {json.dumps(case_id)}]" for last, case_id in calls)
    call = f"[{args}].map(a => {fn}(...a))"
    script = match.group(1) + f"\nconsole.log(JSON.stringify({call}));"
    out = subprocess.run(["node", "-e", script], capture_output=True, encoding="utf-8", check=True)
    return json.loads(out.stdout)


def test_the_link_just_sent_is_shown_for_its_case() -> None:
    assert shown(({"caseId": "case_demo_001", "url": LINK}, "case_demo_001")) == [LINK]


def test_no_link_sent_in_this_page_shows_none() -> None:
    """After a reload the page has no link (it is shown only once), so the view offers Send."""
    assert shown((None, "case_demo_001")) == [None]


def test_another_case_never_shows_the_link() -> None:
    assert shown(({"caseId": "case_demo_001", "url": LINK}, "case_002")) == [None]


def test_no_case_selected_shows_none() -> None:
    assert shown(({"caseId": "case_demo_001", "url": LINK}, None)) == [None]


def test_the_link_box_markup_is_shared_by_the_evidence_page_and_the_tenant_view() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert html.count('<span class="linkbox">') == 1, "one link box template, used by both views"
    assert html.count("linkBox(link, qr)") == 2


# WS2 #56: the QR code kept beside the link (Intake.qr_svg from the same POST).
QR = "data:image/svg+xml;charset=utf-8,%3Csvg%3E%3C/svg%3E"


def test_the_qr_code_shows_with_its_link_for_its_case_only() -> None:
    last = {"caseId": "case_demo_001", "url": LINK, "qr": QR}
    assert shown((last, "case_demo_001"), (last, "case_002"), (last, None), fn="sessionQr") == [
        QR, None, None,
    ]


def test_no_qr_code_without_a_link_or_in_mock_mode() -> None:
    """Mock mode's POST returns no qr_svg, so the link shows alone."""
    assert shown(
        (None, "case_demo_001"),
        ({"caseId": "case_demo_001", "url": LINK}, "case_demo_001"),
        ({"caseId": "case_demo_001", "url": None, "qr": QR}, "case_demo_001"),
        fn="sessionQr",
    ) == [None, None, None]


def test_only_an_svg_data_uri_is_used_as_the_qr_image() -> None:
    calls = [
        ({"caseId": "c1", "url": LINK, "qr": q}, "c1")
        for q in ["javascript:alert(1)", "https://evil.example/qr.svg", "data:text/html,x", 7]
    ]
    assert shown(*calls, fn="sessionQr") == [None, None, None, None]


# The Tenant view previews one case's tenant page, so it is reached from inside a case (the case
# rail and the Evidence page), never from the top bar, where no case is named.


def _function_body(html: str, name: str) -> str:
    start = html.index(f"function {name}(")
    return html[start:html.index("\n}\n", start)]


def test_the_top_bar_has_no_tenant_view_link() -> None:
    chrome = _function_body(INDEX.read_text(encoding="utf-8"), "renderChrome")
    topnav = next(line for line in chrome.splitlines() if '$("#topnav").innerHTML = `' in line
                  and "TENANT_MODE" not in line)
    assert "#intake" not in topnav and "Tenant view" not in topnav


def test_the_case_rail_links_to_the_tenant_view() -> None:
    html = INDEX.read_text(encoding="utf-8")
    assert 'href="#intake"' in _function_body(html, "renderChrome")
    assert 'const IN_CASE = [...CASE_ROUTES, "intake"];' in html


def test_re_sending_a_sent_link_asks_first() -> None:
    html = INDEX.read_text(encoding="utf-8")
    start = html.index('if (a === "resend")')
    handler = html[start:html.index('request("POST", `${C}/intake-link`)', start)]
    assert 'ik.status !== "not_sent" && !confirm(' in handler
