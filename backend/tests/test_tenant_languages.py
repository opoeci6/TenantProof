"""The tenant page's text (web/index.html: LANGS, LANG_EN, I18N, UPLOAD_SOON, NEW_CASE_LANGS),
run under node. Every language the New case form offers has every string, with the same
placeholders as the English; Russian and Hindi are machine-translated and say so (#68, #69)."""

import re
import shutil
from pathlib import Path

import pytest

from tests.node_support import node_json

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")
PLACEHOLDER = re.compile(r"\{\w+\}")
SCRIPT = {"ru": re.compile("[Ѐ-ӿ]"), "hi": re.compile("[ऀ-ॿ]")}


def _statement(source: str, name: str) -> str:
    # I18N spans lines and ends with "};" on its own line; the others are one line.
    pattern = rf"^const {name} = \{{.*?^\}};" if name == "I18N" else rf"^const {name} = [^\n]*$"
    match = re.search(pattern, source, re.S | re.M)
    assert match, f"{name} not found in web/index.html"
    return match.group(0)


@pytest.fixture(scope="module")
def page() -> dict:
    source = INDEX.read_text(encoding="utf-8")
    names = ["LANGS", "LANG_EN", "I18N", "UPLOAD_SOON", "NEW_CASE_LANGS"]
    script = "\n".join(_statement(source, n) for n in names)
    script += "\nconsole.log(JSON.stringify({LANGS, LANG_EN, I18N, UPLOAD_SOON, NEW_CASE_LANGS}));"
    return node_json(script)


def test_every_offered_language_has_every_tenant_string(page: dict) -> None:
    keys = set(page["I18N"]["en"])
    for lang in page["NEW_CASE_LANGS"]:
        assert set(page["I18N"][lang]) == keys, lang
        assert lang in page["UPLOAD_SOON"] and page["LANGS"][lang] and page["LANG_EN"][lang]
        assert not any(v in ("", None) for v in page["I18N"][lang].values()), lang


def test_the_new_case_form_offers_russian_and_hindi(page: dict) -> None:
    assert page["NEW_CASE_LANGS"] == ["en", "zh", "fr", "es", "ru", "hi"]
    assert (page["LANGS"]["ru"], page["LANG_EN"]["ru"]) == ("Русский", "Russian")
    assert (page["LANGS"]["hi"], page["LANG_EN"]["hi"]) == ("हिन्दी", "Hindi")


def test_placeholders_and_list_sizes_match_the_english(page: dict) -> None:
    en = page["I18N"]["en"]
    for lang in page["NEW_CASE_LANGS"]:
        for key, value in en.items():
            if isinstance(value, str):
                got = page["I18N"][lang][key]
                assert sorted(PLACEHOLDER.findall(got)) == sorted(PLACEHOLDER.findall(value)), (
                    lang, key)
        assert len(page["I18N"][lang]["items"]) == len(en["items"]), lang


@pytest.mark.parametrize("lang", ["ru", "hi"])
def test_the_new_strings_are_written_in_their_script(page: dict, lang: str) -> None:
    for key, value in page["I18N"][lang].items():
        for text in value if isinstance(value, list) else [value]:
            assert SCRIPT[lang].search(text) or key == "privacy", (lang, key)
    assert SCRIPT[lang].search(page["UPLOAD_SOON"][lang])


@pytest.mark.parametrize(("lang", "mark"), [
    ("ru", "ru reviewed by Lameda12 2026-11-17"),
    ("hi", "machine-translated, needs native-speaker review"),
])
def test_the_new_string_sets_say_how_far_they_were_checked(lang: str, mark: str) -> None:
    lines = INDEX.read_text(encoding="utf-8").splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith(f"  {lang}: {{ showEn"))
    assert mark in lines[at - 1]
