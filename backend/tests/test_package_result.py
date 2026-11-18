"""The Package page's result panel and Build button (#90). `latestPackage` and `buildButton` are
pure functions in web/index.js, run under node; the server's audit line is what they read after
a page reload, so one test feeds them the real line."""

import hashlib
import json
import shutil

import pytest

from tests.test_empty_states import INDEX, js
from tests.test_package import ATTESTED, BUILD, DOWNLOAD, make_ready

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

SHA_1 = "a" * 64
SHA_2 = "b" * 64
PKG = {
    "version": 1, "sha256": SHA_1, "prepared_by": "Sam Rivera",
    "built_at": "2026-11-18T10:00:00Z",
}  # fmt: skip


def line(version: int, sha: str = SHA_2, actor: str = "caseworker", name: str = "Jo Tran") -> dict:
    return {
        "at": "2026-11-18T11:00:00Z", "actor": actor, "actor_name": name,
        "action": f"Built filing package v{version} (sha256 {sha})",
    }  # fmt: skip


def latest(pkg: dict | None, activity: list[dict]) -> dict | None:
    return js(f"latestPackage({json.dumps(pkg)}, {json.dumps(activity)})")  # type: ignore[return-value]


def test_no_package_yet() -> None:
    assert latest(None, []) is None
    assert js("buildButton(null)") == {"label": "Build package", "hint": ""}


def test_the_package_just_built_is_the_latest() -> None:
    assert latest(PKG, []) == {
        "version": 1, "sha256": SHA_1, "built_by": "Sam Rivera",
        "built_at": "2026-11-18T10:00:00Z",
    }  # fmt: skip


def test_after_a_reload_the_audit_line_gives_the_package() -> None:
    assert latest(None, [line(2)]) == {
        "version": 2, "sha256": SHA_2, "built_by": "Jo Tran", "built_at": "2026-11-18T11:00:00Z",
    }  # fmt: skip


def test_the_highest_version_wins_in_any_order() -> None:
    assert latest(None, [line(1, SHA_1), line(3), line(2)])["version"] == 3  # type: ignore[index]
    assert latest(PKG, [line(2)])["version"] == 2  # a colleague built v2  # type: ignore[index]
    assert latest({**PKG, "version": 4}, [line(2)])["version"] == 4  # type: ignore[index]


@pytest.mark.parametrize("entry", [
    line(9, actor="agent"),
    line(9, actor="tenant"),
    {**line(9), "action": "Proposed event “Built filing package v9”"},
    {**line(9), "action": "Built filing package v9"},
    {**line(9), "action": f"Built filing package v9 (sha256 {SHA_2}) and more"},
])  # fmt: skip
def test_other_activity_is_not_a_package(entry: dict) -> None:
    assert latest(None, [entry]) is None


def test_once_a_package_exists_the_button_makes_a_new_version() -> None:
    assert js('buildButton({version: 2})') == {
        "label": "Build a new version", "hint": "This makes v3. v2 stays available.",
    }  # fmt: skip


def test_the_page_reads_the_line_the_server_writes(fresh_db: None, client) -> None:
    make_ready(client)
    first = client.post(BUILD, json=ATTESTED).json()
    second = client.post(BUILD, json=ATTESTED).json()
    activity = client.get("/api/cases/case_demo_001/activity").json()
    found = latest(None, activity)
    assert found is not None
    assert (found["version"], found["sha256"]) == (2, second["sha256"])
    assert found["built_by"] == second["prepared_by"]
    # The hint's promise: the earlier version can still be downloaded, byte for byte.
    old = client.get(DOWNLOAD, params={"version": 1})
    assert old.status_code == 200 and hashlib.sha256(old.content).hexdigest() == first["sha256"]


def test_the_panel_sits_between_the_button_and_the_contents() -> None:
    source = INDEX.read_text(encoding="utf-8")
    view = source[source.index("function viewPackage()"):]
    button = view.index('data-action="build"')
    panel = view.index("packageResult(latest)", button)
    assert button < panel < view.index("${manifest}", button)
    assert source.count("/package/download?version=") == 1  # one download link, in the panel
