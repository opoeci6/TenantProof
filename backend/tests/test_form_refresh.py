"""How the Form draft refreshes after a save (`formRefreshMode` in web/index.html, a pure
function; issue #161). A refresh used to be dropped whenever another field had focus, so a cleared
row kept its old label. Now it rebuilds everything except the focused control, waits for an IME
composition to end, and still keeps the view while a value is unsaved or a download runs. The
function runs under node."""

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.node_support import node_json

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
BLOCK = re.compile(r"/\* formRefresh:start.*?\*/(.*?)/\* formRefresh:end \*/", re.S)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

IDLE = {"focused": False, "composing": False, "downloading": False, "unsaved": False}


def modes(*cases: dict) -> list[str]:
    match = BLOCK.search(INDEX.read_text(encoding="utf-8"))
    assert match, "formRefresh block not found in web/index.html"
    states = [{**IDLE, **case} for case in cases]
    call = f"{json.dumps(states)}.map(formRefreshMode)"
    return node_json(match.group(1) + f"\nconsole.log(JSON.stringify({call}));")


def test_nothing_focused_replaces_the_view() -> None:
    assert modes({}) == ["full"]


def test_a_focused_next_field_keeps_focus_instead_of_skipping_the_refresh() -> None:
    # The #161 case: the clear of Monthly rent is saved while Weekly rent has focus.
    assert modes({"focused": True}) == ["keep-focus"]


def test_a_composition_in_progress_waits_for_it_to_end() -> None:
    assert modes({"focused": True, "composing": True}) == ["after-composition"]


def test_unsaved_values_and_downloads_still_keep_the_view() -> None:
    # Pending, failed or invalid saves: a new view would show the server's older value.
    assert modes(
        {"unsaved": True},
        {"unsaved": True, "focused": True},
        {"unsaved": True, "composing": True},
        {"downloading": True},
        {"downloading": True, "focused": True},
    ) == ["skip"] * 5


@pytest.mark.parametrize(
    ("rendered_case", "expected_html", "kept"),
    [("case-tenant", "old form", True), ("another-case", "new form", False)],
)
def test_tenant_refresh_preserves_focus_only_in_the_current_draft(
    rendered_case: str, expected_html: str, kept: bool,
) -> None:
    source = INDEX.read_text(encoding="utf-8")
    policy = BLOCK.search(source)
    render = re.search(r"^function render\(\) \{.*?^\}", source, re.M | re.S)
    assert policy and render
    harness = f"""
const CASE_ID = "case-tenant", TENANT_MODE = false;
const S = {{case: {{applicant_role: "tenant"}}, cases: [], form: {{}}}};
const CASE_ROUTES = ["form"], VIEWS = {{form: () => "new form"}};
const route = () => "form", landlordApplication = () => false, renderChrome = () => {{}};
const control = {{id: "fld-weekly_rent", matches: () => true}};
const document = {{activeElement: control}};
const view = {{
  innerHTML: "old form",
  querySelector: () => ({{dataset: {{formDraft: {json.dumps(rendered_case)}}}}}),
  contains: el => el === control,
}};
const $ = () => view;
let formComposing = false, formRenderDeferred = false, kept = false;
const renderAroundFocus = (target, focused, html) => {{
  kept = target === view && focused === control && html === "new form";
  return kept;
}};
"""
    script = (
        harness + policy.group(1) + render.group(0)
        + "\nrender(); console.log(JSON.stringify({html: view.innerHTML, kept}));"
    )
    assert node_json(script) == {"html": expected_html, "kept": kept}
