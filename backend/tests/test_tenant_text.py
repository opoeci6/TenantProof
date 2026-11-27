"""The tenant page's own text (`tCount`, `pickLang` and `uploadErrorText` in web/index.html, pure
functions run under node). #174: with French selected nothing on the tenant page is in English.
Every language in `I18N` has the new keys, "1 fichier sélectionné" is not "1 fichier(s)", and a
415 from the server names the file in the tenant's language instead of showing its English text."""

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.node_support import node_json

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
I18N = re.compile(r"const I18N = (\{.*?\n\});", re.S)
BLOCK = re.compile(r"/\* tenantText:start.*?\*/(.*?)/\* tenantText:end \*/", re.S)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

LANGS = ["en", "zh", "fr", "es", "ru", "hi"]
NEW_KEYS = ["demoPill", "demoPillTitle", "selectedOne", "selectedMany", "badType",
            "uploadFailed", "deadTitle", "deadBody1", "deadBody2"]
FILES = [{"name": "photo.gif"}, {"name": "b.pdf"}]


def js(expression: str) -> object:
    page = INDEX.read_text(encoding="utf-8")
    strings, block = I18N.search(page), BLOCK.search(page)
    assert strings, "I18N not found in web/index.html"
    assert block, "tenantText block not found in web/index.html"
    script = f"const I18N = {strings.group(1)};\n{block.group(1)}"
    return node_json(f"{script}\nconsole.log(JSON.stringify({expression}));")


def strings(code: str) -> dict:
    return js(f"Object.fromEntries(Object.entries(I18N.{code}))")  # type: ignore[return-value]


def upload_text(status: int, message: str, files: list[dict] | None = None,
                lang: str = "fr") -> str:
    err = {"status": status, "message": message}
    files = FILES if files is None else files
    return js(f"uploadErrorText({json.dumps(err)}, {json.dumps(files)}, I18N.{lang})")  # type: ignore[return-value]


# --------------------------------------------------------------------------- one file, three files


@pytest.mark.parametrize(("lang", "n", "text"), [
    ("fr", 1, "1 fichier sélectionné"),
    ("fr", 3, "3 fichiers sélectionnés"),
    ("en", 1, "1 file selected"),
    ("en", 3, "3 files selected"),
    ("zh", 1, "已选择 1 个文件"),
    ("zh", 3, "已选择 3 个文件"),
])
def test_a_count_reads_naturally_in_every_language(lang: str, n: int, text: str) -> None:
    assert js(f"tCount(I18N.{lang}, {n})") == text


# --------------------------------------------------------------------------- every language, keys


def test_every_language_has_every_new_key() -> None:
    for code in LANGS:
        found = strings(code)
        for key in NEW_KEYS:
            assert isinstance(found.get(key), str) and found[key].strip(), (code, key)


def test_the_old_selected_key_is_gone() -> None:
    for code in LANGS:
        assert "selected" not in strings(code), code


def test_send_hint_is_declared_once_per_language() -> None:
    page = INDEX.read_text(encoding="utf-8")
    block = I18N.search(page)
    assert block
    assert block.group(1).count("sendHint:") == len(LANGS)


def test_the_french_intro_reads_with_any_address() -> None:
    intro = str(js("I18N.fr.intro"))
    assert "{unit}" in intro and "au {unit}" not in intro


# --------------------------------------------------------------------------- the browser's language


@pytest.mark.parametrize(("list", "picked"), [
    (["fr-CA", "en"], "fr"),
    (["fr"], "fr"),
    (["de"], "en"),
    ([], "en"),
    (["de-DE", "es-419", "en"], "es"),
])
def test_the_dead_link_page_takes_the_browsers_language(list: list[str], picked: str) -> None:
    assert js(f"pickLang({json.dumps(list)}, I18N)") == picked


# --------------------------------------------------------------------------- the 415


def test_a_415_names_the_file_it_is_about() -> None:
    message = "File 1 is not an accepted type (JPG, PNG, HEIC, PDF or TXT)"
    assert upload_text(415, message) == (
        "« photo.gif » n'est pas un type de fichier accepté (JPG, PNG, HEIC, PDF ou TXT).")
    assert "« b.pdf »" in upload_text(
        415, "File 2 is not an accepted type (JPG, PNG, HEIC, PDF or TXT)")


def test_every_language_names_the_file() -> None:
    for code in LANGS:
        assert "{name}" in str(js(f"I18N.{code}.badType")), code


def test_other_upload_errors_get_the_short_message() -> None:
    assert upload_text(500, "Internal error") == (
        "L'envoi n'a pas fonctionné. Réessayez dans un moment.")
    assert upload_text(415, "File 1 is not an accepted type", files=[]) == (
        "L'envoi n'a pas fonctionné. Réessayez dans un moment.")
    assert upload_text(413, "File 1 is too large", files=FILES) == (
        "L'envoi n'a pas fonctionné. Réessayez dans un moment.")
