"""Counts and empty states on the case pages (`railMeta`, `packageChecks` and `plural` in
web/index.html, pure functions run under node). With no gaps, no form fields or no events there is
nothing to be "done", so the check is neutral grey "Nothing yet", not a green tick; and counts say
"1 file", not "1 files"."""

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.node_support import node_json

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
BLOCK = re.compile(r"/\* pure:start.*?\*/(.*?)/\* pure:end \*/", re.S)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

EMPTY = {"gaps": 0, "total": 0, "reviewed": 0, "blocking": 0, "recommended": 0, "waiting": 0,
         "filled": 0, "fieldsTotal": 0, "reqMissing": 0}


def stats(**kw: int) -> dict:
    return {**EMPTY, **kw}


def js(expr: str) -> object:
    match = BLOCK.search(INDEX.read_text(encoding="utf-8"))
    assert match, "pure block not found in web/index.html"
    script = match.group(1) + f"\nconsole.log(JSON.stringify({expr}));"
    return node_json(script)


def rail(st: dict, files: int = 0) -> dict:
    return js(f"railMeta({json.dumps(st)}, {files})")  # type: ignore[return-value]


def checks(st: dict) -> dict:
    return js(f"packageChecks({json.dumps(st)})")  # type: ignore[return-value]


# --------------------------------------------------------------------------- plurals


@pytest.mark.parametrize(("n", "text"), [(0, "0 files"), (1, "1 file"), (2, "2 files")])
def test_plural(n: int, text: str) -> None:
    assert js(f'plural({n}, "file")') == text


def test_plural_takes_an_irregular_form() -> None:
    assert js('[plural(1, "copy", "copies"), plural(2, "copy", "copies")]') == [
        "1 copy", "2 copies"]


def test_the_rail_says_one_file_not_one_files() -> None:
    assert rail(stats(), 1)["evidence"] == "1 file · from tenant"
    assert rail(stats(), 3)["evidence"] == "3 files · from tenant"


def test_suggestions_and_fields_are_pluralised() -> None:
    one = rail(stats(gaps=1, recommended=1, fieldsTotal=1, filled=1))
    many = rail(stats(gaps=4, recommended=3, fieldsTotal=7, filled=5))
    assert (one["gaps"], one["form"]) == ("1 suggestion", "1 of 1 field")
    assert (many["gaps"], many["form"]) == ("3 suggestions", "5 of 7 fields")


def test_no_count_in_the_page_is_hard_coded_plural() -> None:
    """Every "<n> files / events / tool calls" in a template goes through plural()."""
    source = INDEX.read_text(encoding="utf-8")
    bad = re.findall(r"\$\{[^}]*\} (?:files|events|tool calls|suggestions)\b(?! (?:still|need))",
                     source)
    assert bad == []


# --------------------------------------------------------------------------- empty is neutral


def test_the_rail_says_nothing_yet_when_there_is_nothing() -> None:
    meta = rail(stats())
    assert (meta["gaps"], meta["form"], meta["timeline"]) == ("Nothing yet",) * 3
    assert not (meta["doneGaps"] or meta["doneForm"] or meta["doneTimeline"])


def test_the_package_checks_are_neutral_when_there_is_nothing() -> None:
    result = checks(stats())
    assert {k: (v["state"], v["text"]) for k, v in result.items()} == {
        "missing": ("none", "Nothing yet"), "fields": ("none", "Nothing yet"),
        "timeline": ("none", "Nothing yet"), "suggestions": ("none", "Nothing yet"),
    }


def test_resolved_items_are_still_green() -> None:
    """Items exist and are all dealt with: that is "done", not "nothing"."""
    done = stats(gaps=2, total=3, reviewed=3, fieldsTotal=4, filled=4)
    meta, result = rail(done), checks(done)
    assert meta["doneGaps"] and meta["doneForm"] and meta["doneTimeline"]
    assert meta["gaps"] == "0 suggestions" and meta["form"] == "4 of 4 fields"
    assert {k: v["state"] for k, v in result.items()} == {
        "missing": "yes", "fields": "yes", "timeline": "yes", "suggestions": "yes"}
    assert result["missing"]["text"] == "all resolved" and result["fields"]["text"] == "all filled"


def test_open_items_stay_red_or_amber_and_link_to_the_page() -> None:
    st = stats(gaps=3, blocking=2, waiting=1, recommended=1, total=4, reviewed=1,
               fieldsTotal=5, filled=3, reqMissing=2)
    result = checks(st)
    assert result["missing"] == {"state": "no", "href": "#gaps",
                                 "text": "2 still open, 1 waiting for tenant"}
    assert result["fields"] == {"state": "no", "href": "#form", "text": "2 empty"}
    assert result["timeline"]["state"] == "maybe"
    assert "left out of the form" in result["timeline"]["text"]
    assert result["suggestions"] == {"state": "maybe", "text": "1 open (optional)"}
    assert rail(st)["gaps"] == "2 required open"


def test_a_partly_reviewed_timeline_is_not_done() -> None:
    meta = rail(stats(total=4, reviewed=2))
    assert meta["timeline"] == "2 of 4 reviewed" and not meta["doneTimeline"]


# --------------------------------------------------------------------------- evidence type label


@pytest.mark.parametrize(("kind", "mime", "label"), [
    ("other", "application/pdf", "document"),   # a PDF the server could not place
    ("other", "image/jpeg", "other"),
    ("other", "text/plain", "other"),
    ("lease", "application/pdf", "lease"),       # a better type is kept
    ("receipt", "application/pdf", "receipt"),
    ("letter", "application/pdf", "letter"),
    ("photo", "image/heic", "photo"),
    ("messages", "text/plain", "messages"),
])
def test_kind_label(kind: str, mime: str, label: str) -> None:
    assert js(f"kindLabel({json.dumps({'kind': kind, 'mime': mime})})") == label


# --------------------------------------------------------------------------- run cost line


def test_cost_label_shows_a_number_or_says_unknown() -> None:
    def label(usage: dict) -> list:
        return js(f"[costLabel({json.dumps(usage)}), costNote({json.dumps(usage)})]")  # type: ignore[return-value]

    known = label({"est_cost_usd": 0.0056, "model": "claude-haiku-5-5"})
    assert known == ["Est. cost under $0.01", "At claude-haiku-5-5's list prices"]
    assert label({"est_cost_usd": 0.1255, "model": "claude-sonnet-5-5"})[0] == "Est. cost $0.13"
    unknown, note = label({"est_cost_usd": None, "model": "claude-haiku-4-5"})
    assert unknown == "Est. cost unknown"
    assert "claude-haiku-4-5" in note and "$" not in unknown
    assert label({"est_cost_usd": 0, "model": None})[0] == "Est. cost under $0.01"  # zero is known
    assert label({"model": "x"})[0] == "Est. cost unknown"  # a missing value is unknown too


# --------------------------------------------------------------------------- deadline wording


@pytest.mark.parametrize(("days", "text"), [
    (7, "Must be received in 7 days"),
    (1, "Must be received in 1 day"),
    (0, "Must be received today"),
    (-1, "Deadline passed (1 day ago)"),
    (-2, "Deadline passed (2 days ago)"),
])
def test_deadline_phrase_matches_the_server(days: int, text: str) -> None:
    from app.services.priority import deadline_phrase

    assert js(f"deadlinePhrase({days})") == text == deadline_phrase(days)


@pytest.mark.parametrize("stored", [
    "Evidence due · hearing 2026-11-21", "evidence due · hearing 2026-11-21",
    "Must be received by · hearing 2026-11-21",
])
def test_a_stored_deadline_label_reads_in_todays_wording(stored: str) -> None:
    assert js(f"deadlineLabel({json.dumps(stored)})") == "Must be received by · hearing 2026-11-21"
