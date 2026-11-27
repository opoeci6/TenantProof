"""What the Run page shows in a step summary or a tool call (`humanRunText` in web/index.html, a
pure function run under node). #175: a judge should never see `gap_942b3e6f0f`, `run_7a88868010`
or `landlord_address` on the Run page: a gap becomes its title, a run or source id goes, and a
Form J field key becomes its label. The data contract block keeps the text as the server wrote
it; only the Package page's audit log also drops the run id, and nothing else."""

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.node_support import node_json

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
BLOCK = re.compile(r"/\* runText:start.*?\*/(.*?)/\* runText:end \*/", re.S)
LANG_EN = re.compile(r"const LANG_EN = \{.*?\};", re.S)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

FIELDS = [
    {"key": "tenant_name", "label": "Tenant name"},
    {"key": "landlord_address", "label": "Landlord address"},
    {"key": "monthly_rent", "label": "Monthly rent"},
]
GAPS = [
    {"id": "gap_942b3e6f0f", "title": "Landlord's address for service"},
    {"id": "gap_0a1b2c3d4e", "title": "Original photo for E-05"},
]


def evaluate(expression: str) -> object:
    page = INDEX.read_text(encoding="utf-8")
    block = BLOCK.search(page)
    assert block, "runText block not found in web/index.html"
    lang = LANG_EN.search(page)
    assert lang, "LANG_EN not found in web/index.html"
    # The block names a language with LANG_EN, which the page defines above it.
    script = lang.group(0) + block.group(1) + f"\nconsole.log(JSON.stringify({expression}));"
    return node_json(script)


def human(text: str, fields: list[dict] | None = None, gaps: list[dict] | None = None) -> str:
    args = {"fields": FIELDS if fields is None else fields, "gaps": GAPS if gaps is None else gaps}
    return evaluate(f"humanRunText({json.dumps(text)}, {json.dumps(args)})")  # type: ignore[return-value]


# --------------------------------------------------------------------------- ids


def test_a_gap_id_becomes_its_title_in_quotes() -> None:
    assert human("Created gap_942b3e6f0f for landlord_address") == (
        'Created "Landlord\'s address for service" for Landlord address')


def test_a_gap_id_the_page_does_not_know_says_a_gap() -> None:
    assert human("Drafted a request for gap_ffffffffff") == "Drafted a request for a gap"


def test_a_gap_id_inside_json_is_replaced_without_doubled_quotes() -> None:
    assert human('{"gap_id": "gap_942b3e6f0f", "severity": "blocking"}') == (
        '{"gap_id": "Landlord\'s address for service", "severity": "blocking"}')


def test_run_and_source_ids_go() -> None:
    assert human("Finished run_7a88868010: 9 events, 2 required items") == (
        "Finished: 9 events, 2 required items")
    assert human('{"source_id": "src_03", "title": "Form J"}') == (
        '{"source_id": "", "title": "Form J"}')


def test_no_id_or_schema_key_survives_a_typical_tool_line() -> None:
    text = ('{"gap_id": "gap_0a1b2c3d4e", "field_key": "monthly_rent", '
            '"source_id": "src_03", "run_id": "run_7a88868010"}')
    shown = human(text)
    # The id values go; the JSON property names (gap_id, source_id) are not ids.
    assert not re.search(r"\b(?:run_[0-9a-f]{2,}|gap_[0-9a-f]{2,}|src_[A-Za-z0-9_]+)", shown), shown
    assert "monthly_rent" not in shown


def test_the_audit_log_drops_only_the_run_id() -> None:
    assert evaluate("stripRunIds('Finished run_001: 9 events, 2 required items')") == (
        "Finished: 9 events, 2 required items")
    assert evaluate("stripRunIds('Accepted event “Tenant reports the leak” and added a note')") == (
        "Accepted event “Tenant reports the leak” and added a note")


def test_a_link_is_left_alone() -> None:
    """An id or a key inside a URL is part of the address, not text to rewrite."""
    url = "https://example.test/files/src_03/gap_942b3e6f0f?run=run_7a88868010"
    assert human(f"See {url} for the file") == f"See {url} for the file"
    assert evaluate(f"stripRunIds({json.dumps('See ' + url)})") == f"See {url}"


# --------------------------------------------------------------------------- field keys


def test_a_field_key_becomes_its_label() -> None:
    assert human("Filled tenant_name and monthly_rent") == "Filled Tenant name and Monthly rent"


def test_a_field_key_in_json_becomes_its_label() -> None:
    assert human('{"field_key": "landlord_address", "reason": "not on the lease"}') == (
        '{"field_key": "Landlord address", "reason": "not on the lease"}')


def test_a_one_word_key_is_an_ordinary_word_and_stays() -> None:
    fields = [*FIELDS, {"key": "remedy", "label": "What the tenant is asking for"}]
    text = "The remedy is the tenant's choice; left remedy empty. Filled monthly_rent."
    assert human(text, fields=fields) == (
        "The remedy is the tenant's choice; left remedy empty. Filled Monthly rent.")


def test_a_key_the_form_does_not_have_stays_as_written() -> None:
    assert human("hearing_date is still missing") == "hearing_date is still missing"


def test_without_a_form_or_gaps_nothing_breaks() -> None:
    assert human("landlord_address and gap_942b3e6f0f", [], []) == (
        "landlord_address and a gap")


def test_a_label_that_contains_another_key_is_not_replaced_twice() -> None:
    fields = [{"key": "rent", "label": "Monthly rent"}, {"key": "monthly_rent", "label": "Rent"}]
    assert human("monthly_rent", fields=fields) == "Rent"


# --------------------------------------------------------------------------- language codes


def test_a_language_code_in_a_summary_becomes_its_english_name() -> None:
    assert human("Drafted the request in language zh") == "Drafted the request in language Chinese"
    assert human("Wrote the request in French (fr)") == "Wrote the request in French (French)"


def test_a_word_that_contains_zh_or_fr_is_left_alone() -> None:
    for text in ["The zhejiang front door", "A friendly reminder", "zhihu.com is not a source"]:
        assert human(text) == text
