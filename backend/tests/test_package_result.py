"""The Package page's result panel and Build button (#90). `latestPackage` and `buildButton` are
pure functions in web/index.html, run under node; the server's audit line is what they read after
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


def versions(pkg: dict | None, activity: list[dict]) -> list[dict]:
    return js(f"packageVersions({json.dumps(pkg)}, {json.dumps(activity)})")  # type: ignore[return-value]


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


# --- #199: every version kept, each with its own download --------------------------------


def test_versions_are_newest_first_and_listed_once() -> None:
    found = versions(None, [line(1, SHA_1), line(2), line(1, SHA_1)])
    assert [v["version"] for v in found] == [2, 1]
    assert found[0] == {"version": 2, "sha256": SHA_2, "built_by": "Jo Tran",
                        "built_at": "2026-11-18T11:00:00Z"}


def test_the_package_just_built_wins_over_its_audit_line() -> None:
    pkg = {**PKG, "version": 1, "sha256": SHA_2, "prepared_by": "Sam Rivera"}
    assert versions(pkg, [line(1, SHA_1)]) == [{
        "version": 1, "sha256": SHA_2, "built_by": "Sam Rivera", "built_at": PKG["built_at"],
    }]


def test_a_version_only_the_audit_log_knows_is_kept() -> None:
    assert versions(PKG, [line(2)]) == [
        {"version": 2, "sha256": SHA_2, "built_by": "Jo Tran", "built_at": "2026-11-18T11:00:00Z"},
        {"version": 1, "sha256": SHA_1, "built_by": "Sam Rivera", "built_at": PKG["built_at"]},
    ]


@pytest.mark.parametrize("entry", [
    line(9, actor="agent"),
    line(9, actor="tenant"),
    {**line(9), "action": "Built filing package v9"},
    {**line(9), "action": f"Built filing package v9 (sha256 {SHA_2}) and more"},
    {**line(9), "action": "Built the filing package"},
])
def test_other_activity_is_not_a_version(entry: dict) -> None:
    assert versions(None, [entry]) == []


def test_no_package_is_no_versions() -> None:
    assert versions(None, []) == []
    assert latest(None, []) is None


def test_the_page_lists_earlier_versions_with_their_own_download() -> None:
    source = INDEX.read_text(encoding="utf-8")
    view = source[source.index("function viewPackage()"):source.index("function reqHtml(")]
    assert "const earlier = packageVersions(S.pkg, S.activity).slice(1);" in view
    assert '<h2 class="section-h">Earlier versions</h2>' in view
    assert "packageDownloadUrl(v.version)" in view
    assert "packageDownloadUrl(p.version)" in source  # the result panel uses the same helper
    assert source.count("/package/download?version=") == 1


# --- #201: a built package says when the case moved on -----------------------------------


def entry(action: str, at: str, actor: str = "caseworker", name: str = "Sam Rivera") -> dict:
    return {"at": at, "actor": actor, "actor_name": name, "action": action}


def built(version: int, at: str = "2026-11-18T11:00:00Z", sha: str = SHA_2) -> dict:
    return entry(f"Built filing package v{version} (sha256 {sha})", at)


def is_change(action: str) -> bool:
    return js(f"isPackageChangeAction({json.dumps(action)})")  # type: ignore[return-value]


def changes(pkg: dict | None, activity: list[dict] | None) -> list[dict]:
    return js(f"packageChangesAfter({json.dumps(pkg)}, {json.dumps(activity)})")  # type: ignore[return-value]


@pytest.mark.parametrize("action", [
    "Edited form field: Monthly rent",
    "Updated form field “Monthly rent”",
    "Drafted form field “Monthly rent”",
    "Added the 12 Form J fields from the lease",
    "Resolved item: Landlord's address for service",
    "Reopened item: Landlord's address for service",
    "Resolved “Landlord's address for service”",
    "Reopened “Landlord's address for service”",
    "Confirmed the files are genuine and agreed to share them",
    # The exact line services/evidence/ingest.py writes when tenant files arrive after a
    # confirmation.
    "New files arrived after the confirmation, so it must be given again",
    "Accepted event “Tenant reports the leak”",
    "Accepted timeline event: Tenant reports the leak",
    "Edited event “Tenant reports the leak”",
    "Rejected event “Tenant reports the leak”",
    "Reset event “Tenant reports the leak”",
    "Reopened event “Tenant reports the leak”",
    "Uploaded 7 files and confirmed they are genuine (Chinese)",
    "Added 2 files",
    "Added 1 file",
    "Finished run_001: 9 events, 2 required items, 4 tenant requests drafted",
    "Removed 3 unreviewed events",
    "Extracted facts: 5 from the lease",
])
def test_export_relevant_actions_make_a_build_out_of_date(action: str) -> None:
    assert is_change(action) is True


@pytest.mark.parametrize("action", [
    "Noted on timeline event: Tenant reports the leak",
    "Sent a request to the tenant: “Landlord's address for service”",
    "Edited the request to the tenant: “Landlord's address for service”",
    "Approved and sent a request to the tenant: “Landlord's address for service”",
    "Created the intake link",
    "Sent the intake link in French",  # routers/cases.py: the first send says "Sent"
    "Re-sent the intake link in Chinese (中文)",
    "Translated a quoted source",
    "Hid a personal detail from the AI model",
    "Let a personal detail go to the AI model as written",
    "Undid a personal-detail choice",
    "Checked the official sources: 2 unchanged",
    "Dismissed item: Original photo for E-05",
    "Marked waiting on tenant",
    f"Built filing package v2 (sha256 {SHA_2})",
    "Built the filing package",
    "Started run_001",
    "Closed run_001",
    "Paused run_001",
    "Answered the agent's question on run_001",
    "Something nobody has written yet",
])
def test_internal_actions_do_not_make_a_build_out_of_date(action: str) -> None:
    assert is_change(action) is False


def test_a_form_edit_after_the_build_is_a_change() -> None:
    activity = [entry("Edited form field: Monthly rent", "2026-11-18T12:00:00Z"), built(2)]
    found = changes({"version": 2, "built_at": "2026-11-18T11:00:00Z"}, activity)
    assert [a["action"] for a in found] == ["Edited form field: Monthly rent"]


def test_activity_before_the_build_is_not_a_change() -> None:
    activity = [built(2), entry("Edited form field: Monthly rent", "2026-11-18T10:00:00Z")]
    assert changes({"version": 2, "built_at": "2026-11-18T11:00:00Z"}, activity) == []


def test_the_same_timestamp_is_read_from_the_order_of_the_log() -> None:
    """Activity is newest first, so a line above the build came after it."""
    same = "2026-11-18T11:00:00Z"
    edit = entry("Edited form field: Monthly rent", same)
    assert len(changes({"version": 2, "built_at": same}, [edit, built(2, same)])) == 1
    assert changes({"version": 2, "built_at": same}, [built(2, same), edit]) == []


def test_a_new_build_clears_the_warning_until_the_next_edit() -> None:
    activity = [
        built(3, "2026-11-18T13:00:00Z", SHA_1),
        entry("Edited form field: Monthly rent", "2026-11-18T12:00:00Z"),
        built(2, "2026-11-18T11:00:00Z"),
    ]
    assert changes({"version": 3, "built_at": "2026-11-18T13:00:00Z"}, activity) == []
    assert len(changes({"version": 2, "built_at": "2026-11-18T11:00:00Z"}, activity)) == 1


def test_an_internal_note_after_the_build_is_not_a_change() -> None:
    note = entry("Noted on timeline event: Tenant reports the leak", "2026-11-18T12:00:00Z")
    assert changes({"version": 2, "built_at": "2026-11-18T11:00:00Z"}, [note, built(2)]) == []


def test_without_a_package_or_a_log_nothing_is_out_of_date() -> None:
    assert changes(None, [entry("Edited form field: Monthly rent", "2026-11-18T12:00:00Z")]) == []
    assert changes({"version": 1, "built_at": "2026-11-18T11:00:00Z"}, None) == []


def test_the_page_says_a_built_package_is_out_of_date() -> None:
    source = INDEX.read_text(encoding="utf-8")
    panel = source[source.index("function packageResult("):
                   source.index("function showPackageResult(")]
    assert 'pkg-result${stale ? " is-stale" : ""}' in panel
    assert 'Package v${p.version} is ${stale ? "out of date" : "ready"}' in panel
    assert "function packageResult(p, changes = packageChangesAfter(p, S.activity))" in panel
    assert ".pkg-result.is-stale{border-color:var(--warn)}" in source
    view = source[source.index("function viewPackage()"):source.index("function reqHtml(")]
    assert 'class="btn${!latest || stale ? " primary" : ""}"' in view
    chrome = source[source.index("function renderChrome()"):source.index("function newCaseForm(")]
    assert 'pkgStale ? "Out of date" : "Built"' in chrome
