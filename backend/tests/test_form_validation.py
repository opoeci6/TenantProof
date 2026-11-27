"""Form J client validation rules (`moneyFieldError` and `dateOrderError` in web/index.html, run
under node; #195) and the inline field-error lifecycle around refreshes and focus moves."""

import json
import re
import shutil

import pytest

from app.services.form_values import DATE_ORDER_PAIRS
from tests.node_support import node_json
from tests.test_empty_states import INDEX

BLOCK = re.compile(r"/\* formValidation:start.*?\*/(.*?)/\* formValidation:end \*/", re.S)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def call(expr: str) -> object:
    source = INDEX.read_text(encoding="utf-8")
    match = BLOCK.search(source)
    assert match, "formValidation block not found in web/index.html"
    return node_json(match.group(1) + f"\nconsole.log(JSON.stringify({expr}));")


def money(key: str, value: str, saved: str = "") -> str | None:
    res = call(f"moneyFieldError({json.dumps(key)}, {json.dumps(value)}, {json.dumps(saved)})")
    assert res is None or isinstance(res, str)
    return res


def date_order(key: str, value: str, values: dict[str, str], saved: str = "") -> str | None:
    res = call(
        f"dateOrderError({json.dumps(key)}, {json.dumps(value)}, "
        f"{json.dumps(saved)}, {json.dumps(values)})"
    )
    assert res is None or isinstance(res, str)
    return res


@pytest.mark.parametrize("key", ["monthly_rent", "weekly_rent", "deposit_amount"])
@pytest.mark.parametrize("bad", ["-1", "0", "0.00", "abc", "$1,200.555"])
def test_bad_cad_amounts_give_the_inline_reason(key: str, bad: str) -> None:
    err = money(key, bad)
    assert err is not None and err.startswith("Not saved.") and "positive amount in CAD" in err


@pytest.mark.parametrize("key", ["monthly_rent", "weekly_rent", "deposit_amount"])
@pytest.mark.parametrize("good", ["1200", "$1,200.50", "CAD 500", "500.00 CAD", ""])
def test_valid_and_blank_cad_amounts_pass(key: str, good: str) -> None:
    assert money(key, good) is None


def test_unchanged_stored_amount_stays_confirmable() -> None:
    assert money("deposit_amount", "-1", saved="-1") is None
    assert money("tenant_name", "-1") is None


@pytest.mark.parametrize(
    ("key", "value", "values", "expected"),
    [
        ("move_out_date", "2026-04-01", {"move_in_date": "2026-04-03"}, "move-in date"),
        ("move_in_date", "2026-04-05", {"move_out_date": "2026-04-03"}, "move-in date"),
        ("fixed_term_end", "2026-05-01", {"fixed_term_start": "2026-05-02"}, "start date"),
        ("fixed_term_start", "2026-05-05", {"fixed_term_end": "2026-05-02"}, "start date"),
    ],
)
def test_contradictory_date_pair_is_refused_in_both_directions(
    key: str, value: str, values: dict[str, str], expected: str,
) -> None:
    err = date_order(key, value, values)
    assert err is not None and err.startswith("Not saved.") and expected in err


@pytest.mark.parametrize(
    ("key", "value", "values"),
    [
        ("move_out_date", "2026-04-03", {"move_in_date": "2026-04-03"}),
        ("fixed_term_end", "2026-05-02", {"fixed_term_start": "2026-05-02"}),
        ("move_out_date", "", {"move_in_date": "2026-04-03"}),
        ("move_in_date", "2026-04-03", {}),
        ("move_out_date", "1899-12-31", {"move_in_date": "2026-04-03"}),
        ("move_out_date", "2026-04-03", {"move_in_date": "1899-12-31"}),
        ("notice_effective_date", "2026-04-01", {"notice_given_date": "2026-04-03"}),
    ],
)
def test_allowed_date_pairs_and_blanks_pass(
    key: str, value: str, values: dict[str, str],
) -> None:
    assert date_order(key, value, values) is None


def test_unchanged_legacy_reversed_date_remains_confirmable() -> None:
    values = {"move_in_date": "2026-04-03"}
    assert date_order("move_out_date", "2026-04-01", values, saved="2026-04-01") is None


def test_date_order_messages_match_backend_form_values() -> None:
    js_pairs = call("DATE_ORDER_PAIRS")
    assert isinstance(js_pairs, list)
    assert {
        (start, end): message for start, end, message in js_pairs
    } == DATE_ORDER_PAIRS


def test_field_error_helpers_resolve_live_node_and_clear_before_refresh() -> None:
    source = INDEX.read_text(encoding="utf-8")
    helpers = source[
        source.index("const fieldNode = "):source.index("async function saveDraftControl(")
    ]
    assert "input && input.isConnected ? input : document.getElementById(`fld-${key}`)" in helpers
    assert helpers.count("const el = fieldNode(key, input);") == 2
    assert helpers.count("document.getElementById(`err-${key}`)") == 2

    save = source[source.index("async function saveDraftControl("):
                  source.index("async function downloadFormDraft(")]
    saved_idx = save.index("await saveDraftField(key, value);")
    assert save.index("clearFieldError(key, input);") < saved_idx
    assert save.index("setFieldError(key, msg") > saved_idx

    commit = source[source.index("async function commitDateField("):
                    source.index("async function downloadFormDraft(")]
    commit_saved = commit.index("await saveDraftField(key, input.value);")
    assert commit.index("clearFieldError(key, input);") < commit_saved
    assert commit.index("setFieldError(key, msg") > commit_saved


def test_date_save_reads_data_saved_and_form_marks_invalid_fields() -> None:
    source = INDEX.read_text(encoding="utf-8")
    view = source[source.index("function viewForm()"):source.index("/* #90: the result of a build")]
    assert 'data-saved="${esc(legacyDate ? "" : saved)}"' in view
    assert 'class="field${fieldErr ? " is-invalid" : ""}"' in view
    assert 'aria-invalid="${fieldErr ? "true" : "false"}"' in view
    assert 'class="field-err" id="err-${esc(x.key)}" role="alert"' in view

    focusout = source[source.index('document.addEventListener("focusout"'):
                       source.index('document.addEventListener("change"')]
    assert "dateState.confirmed.get(key) ?? input.dataset.saved" in focusout

    hashchange = source[source.index('window.addEventListener("hashchange"'):
                         source.index("/* #179:")]
    assert "S.draftSaves.errors?.clear()" in hashchange
