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


def pure(expr: str) -> object:
    """Runs one expression against the block's pure helpers."""
    match = BLOCK.search(INDEX.read_text(encoding="utf-8"))
    assert match, "formRefresh block not found in web/index.html"
    return node_json(match.group(1) + f"\nconsole.log(JSON.stringify({expr}));")


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


# --------------------------------------------------------------------------- #194

OLD = ["fld-deposit_paid", "fld-deposit_amount", "fld-notice_given"]


def test_the_next_surviving_control_is_the_one_to_focus() -> None:
    assert pure('nextSurvivingControlId(["a", "b", "c"], "b", ["a", "c"])') == "c"


def test_the_previous_control_takes_over_when_the_last_one_disappears() -> None:
    assert pure('nextSurvivingControlId(["a", "b", "c"], "c", ["a", "b"])') == "b"


def test_no_control_is_focused_when_none_survives() -> None:
    assert pure('nextSurvivingControlId(["a", "b"], "a", [])') is None
    assert pure('nextSurvivingControlId(["a", "b"], "a", null)') is None


def test_a_control_that_was_not_in_the_old_view_has_no_successor() -> None:
    assert pure('nextSurvivingControlId(["a", "b"], "elsewhere", ["a"])') is None


def disappearing_control(*, value: str, saved: str) -> str:
    """A render of the Form draft whose focused control the new view no longer has: the parent
    answer (Security deposit paid -> No) was just saved, and Tab moved focus into the child."""
    source = INDEX.read_text(encoding="utf-8")
    policy = BLOCK.search(source)
    render = re.search(r"^function render\(\) \{.*?^\}", source, re.M | re.S)
    assert policy and render
    harness = f"""
const CASE_ID = "case-tenant", TENANT_MODE = false;
const S = {{case: {{applicant_role: "tenant"}}, cases: [], form: {{}}, draftSaves: null}};
const CASE_ROUTES = ["form"], VIEWS = {{form: () => "new form"}};
const route = () => "form", landlordApplication = () => false, renderChrome = () => {{}};
const control = {{
  id: "fld-deposit_amount", matches: () => true,
  value: {json.dumps(value)}, dataset: {{saved: {json.dumps(saved)}}},
}};
const next = {{id: "fld-notice_given", focus: () => {{focused = next.id;}}}};
let focused = null;
const document = {{activeElement: control, getElementById: id => (id === next.id ? next : null)}};
let current = {json.dumps(OLD)};
const view = {{
  _html: "old form",
  get innerHTML() {{ return this._html; }},
  set innerHTML(html) {{ this._html = html; current = {json.dumps([i for i in OLD if i != "fld-deposit_amount"])}; }},
  querySelector: () => ({{dataset: {{formDraft: "case-tenant"}}}}),
  contains: el => el === control,
  querySelectorAll: () => current.map(id => ({{id}})),
}};
const $ = () => view;
let formComposing = false, formRenderDeferred = false;
const renderAroundFocus = () => false;  // the focused control is not in the new view
"""
    script = (
        harness + policy.group(1) + render.group(0)
        + "\nrender(); console.log(JSON.stringify({html: view.innerHTML, focused, formRenderDeferred}));"
    )
    return node_json(script)


def test_an_untouched_control_that_disappears_is_replaced_and_focus_moves_on() -> None:
    """Tabbed into Security deposit amount, not typed into: the row goes and focus lands on the
    next control, so the hidden field is never left on the page (#194)."""
    assert disappearing_control(value="500", saved="500") == {
        "html": "new form", "focused": "fld-notice_given", "formRenderDeferred": False,
    }


def test_a_typed_into_control_keeps_the_view_until_it_loses_focus() -> None:
    """Typed into and not saved yet: the old view stays so the text is not lost, and the blur
    completes the reconciliation."""
    assert disappearing_control(value="300", saved="500") == {
        "html": "old form", "focused": None, "formRenderDeferred": True,
    }
