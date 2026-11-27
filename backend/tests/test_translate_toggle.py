"""Tests for `englishToggle` in web/index.html, run under node (#171)."""

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.node_support import node_json

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
BLOCK = re.compile(r"/\* translateToggle:start \*/(.*?)/\* translateToggle:end \*/", re.S)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def toggle(text: str | None, language: str | None) -> str:
    match = BLOCK.search(INDEX.read_text(encoding="utf-8"))
    assert match, "translateToggle block not found in web/index.html"
    call_js = f"englishToggle({json.dumps(text)}, {json.dumps(language)})"
    script = (
        "const esc = s => String(s ?? '');\n"
        + match.group(1)
        + f"\nconsole.log(JSON.stringify({call_js}));"
    )
    return node_json(script)


@pytest.mark.parametrize("language", ["fr", "fr-CA", "es"])
def test_ascii_quote_in_non_english_case_shows_button(language: str) -> None:
    html = toggle("Notre plombier vous contactera avant le 5 septembre.", language)
    assert "Show in English" in html
    assert f'data-language="{language}"' in html


def test_accented_quote_in_french_case_shows_button() -> None:
    assert "Show in English" in toggle("Réparation demandée", "fr")


@pytest.mark.parametrize("language", ["en", "en-CA", "", None])
def test_ascii_quote_in_english_or_unset_case_shows_no_button(language: str | None) -> None:
    assert toggle("Our plumber will contact you before September 5.", language) == ""


@pytest.mark.parametrize("text", ["12:30", "189,99 $", "---", "", None])
def test_no_letter_excerpt_in_french_case_shows_no_button(text: str | None) -> None:
    assert toggle(text, "fr") == ""


@pytest.mark.parametrize("text", ["房东说下周一修", "café"])
def test_non_ascii_quote_in_english_case_keeps_button(text: str) -> None:
    assert "Show in English" in toggle(text, "en")
