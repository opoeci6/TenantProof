"""The document shell, the caseload's handling of an unknown `?case=` and the URL signing out
leaves behind (`web/index.html`). The markup checks read the file; the behaviour checks run the
page's pure `caseInUrl` block under node. Issues #203, #214, #215, #216, #217, #219."""

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.node_support import node_json

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
CASE_IN_URL = re.compile(r"/\* caseInUrl:start.*?\*/(.*?)/\* caseInUrl:end \*/", re.S)
RUN_POLLING = re.compile(r"/\* runPolling:start.*?\*/(.*?)/\* runPolling:end \*/", re.S)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

HARNESS = """
const USE_MOCK = false, TENANT_MODE = false;
const location = { search: "?case=wanted", href: "https://clinic.test/?case=c1#timeline" };
const history = { state: null, replaceState: () => {} };
"""


def source() -> str:
    return INDEX.read_text(encoding="utf-8")


def js(expr: str) -> object:
    match = CASE_IN_URL.search(source())
    assert match, "caseInUrl block not found in web/index.html"
    return node_json(HARNESS + match.group(1) + f"\nconsole.log(JSON.stringify({expr}));")


# --------------------------------------------------------------------------- #214, #215, #216


def test_the_document_declares_its_language() -> None:
    """WCAG 3.1.1: the shell says which language its text is in. The tenant page overrides it."""
    first = source().splitlines()[0]
    assert first.startswith("<!doctype html>")
    assert '<html lang="en">' in first


def test_the_title_and_the_viewport_meta_live_in_the_head() -> None:
    html = source()
    head_end = html.index("</head>")
    head = html[:head_end]
    assert "<title>TenantProof</title>" in head
    assert '<meta name="claude:viewport-fit-sides" content="cover">' in head
    body = html[html.index("<body>"):]
    assert "<title>" not in body
    assert 'name="claude:viewport-fit-sides"' not in body


def test_light_theme_muted_text_meets_wcag_aa_and_the_dark_theme_is_unchanged() -> None:
    html = source()
    assert "--ink-3:#5F6C68" in html
    assert "--ink-3:#85928D" in html
    assert "--ink-3:#6A7773" not in html


# --------------------------------------------------------------------------- #203


def test_the_audit_log_wraps_a_long_hash_inside_its_column() -> None:
    html = source()
    rule = re.search(r"\.log li > span\{([^}]*)\}", html)
    assert rule, ".log li > span rule not found in web/index.html"
    assert "min-width:0" in rule.group(1).replace(" ", "")
    assert "overflow-wrap:anywhere" in rule.group(1).replace(" ", "")


# --------------------------------------------------------------------------- #217


def test_an_unknown_case_in_the_url_is_not_swapped_for_another() -> None:
    cases = [{"id": "case_a", "has_demo_data": True}, {"id": "case_b", "has_demo_data": False}]
    assert js(f"pickInitialCase({json.dumps(cases)}, 'case_missing')") == {
        "id": None, "missing": "case_missing",
    }


def test_a_known_case_in_the_url_is_opened() -> None:
    cases = [{"id": "case_a", "has_demo_data": True}, {"id": "case_b", "has_demo_data": False}]
    assert js(f"pickInitialCase({json.dumps(cases)}, 'case_b')") == {
        "id": "case_b", "missing": None,
    }


def test_without_a_case_in_the_url_the_first_demo_case_opens() -> None:
    cases = [{"id": "case_a", "has_demo_data": False}, {"id": "case_b", "has_demo_data": True}]
    assert js(f"pickInitialCase({json.dumps(cases)}, null)") == {"id": "case_b", "missing": None}
    assert js(f"pickInitialCase({json.dumps(cases)}, '')") == {"id": "case_b", "missing": None}


def test_a_caseload_without_demo_data_opens_its_first_case() -> None:
    cases = [{"id": "case_a"}, {"id": "case_b"}]
    assert js(f"pickInitialCase({json.dumps(cases)}, null)") == {"id": "case_a", "missing": None}
    assert js("pickInitialCase([], null)") == {"id": None, "missing": None}


def test_the_caseload_shows_the_missing_case_notice() -> None:
    """The notice sits in both caseload branches: with rows and with an empty caseload."""
    html = source()
    assert html.count("That case was not found or you do not have access.") == 1
    notice = re.search(r'const missingNote = S\.missingCase \? `([^`]*)`', html)
    assert notice and 'role="alert"' in notice.group(1)
    assert html.count("${missingNote}${newCaseForm()}") == 2


# --------------------------------------------------------------------------- #219


@pytest.mark.parametrize(("href", "left"), [
    ("https://clinic.test/?case=c1#timeline", "/"),
    ("https://clinic.test/#gaps", "/"),
    ("https://clinic.test/index.html?mock=1#form", "/index.html?mock=1"),
    ("https://clinic.test/?case=c1&mock=1#package", "/?mock=1"),
])
def test_signing_out_leaves_the_page_without_the_case_or_the_hash(href: str, left: str) -> None:
    assert js(f"signOutUrl({json.dumps(href)})") == left


# --------------------------------------------------------------------------- #218

LOADERS_HARNESS = """
let CASE_ID = "case-a", TENANT_MODE = false;
const C = "/cases/case-a";
const calls = [];
const S = { run: "kept", pkg: "kept", case: null };
const request = async (method, path) => { calls.push(path); return null; };
"""

def loaders(script: str) -> object:
    """Runs `script` with the page's real LOADERS in scope and returns what it prints."""
    html = source()
    block = html[html.index("const LOADERS = {"):html.index("function prioritizeClient(")]
    assert "run: async () => {" in block and "pkg: async () => {" in block
    return node_json(LOADERS_HARNESS + block + f"\n(async () => {{{script}}})();")


@pytest.mark.parametrize(("case", "paths"), [
    ({"id": "case-a", "applicant_role": "tenant", "has_run": False, "has_package": False}, []),
    ({"id": "case-a", "applicant_role": "landlord"}, []),
    ({"id": "case-a", "applicant_role": "tenant", "has_run": True, "has_package": True},
     ["/cases/case-a/runs/latest", "/cases/case-a/package"]),
])
def test_a_case_is_asked_for_its_run_and_package_only_when_it_has_them(
    case: dict, paths: list[str],
) -> None:
    """#218: no request means no 404 or 409 in the browser console; the endpoints are unchanged."""
    info = loaders(f"""
      S.case = {json.dumps(case)};
      await LOADERS.run();
      await LOADERS.pkg();
      console.log(JSON.stringify({{calls, run: S.run, pkg: S.pkg}}));
    """)
    assert info["calls"] == paths  # type: ignore[index]
    assert info["run"] is None and info["pkg"] is None  # type: ignore[index]


def test_the_case_is_loaded_before_its_run_and_package() -> None:
    """`loadAndRender` reads "case" first, so the flags are there when "run" and "pkg" decide."""
    match = RUN_POLLING.search(source())
    assert match, "runPolling block not found in web/index.html"
    harness = """
    let CASE_ID = "case-a", TENANT_MODE = false, CASELESS_LOADERS = ["me", "cases"];
    const order = [], S = { case: null, run: null, pkg: null };
    const LOADERS = {
      case: async () => { order.push("case"); S.case = {id: "case-a"}; },
      run: async () => { order.push("run"); },
      pkg: async () => { order.push("pkg"); },
      me: async () => { order.push("me"); },
      cases: async () => { order.push("cases"); },
      activity: async () => { order.push("activity"); },
    };
    const render = () => {}, renderChrome = () => {}, route = () => "package";
    const $ = () => ({}), toast = () => {}, refresh = async () => {}, request = () => null;
    """
    info = node_json(harness + match.group(1) + """
      (async () => {
        await loadAndRender(["case", "run", "pkg"]);
        console.log(JSON.stringify({order}));
      })();
    """)
    order = info["order"]  # type: ignore[index]
    assert order[0] == "case"
    assert order.index("case") < order.index("run") < order.index("pkg")
