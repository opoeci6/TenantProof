"""What the run page's rail and pill say (`runState` in web/index.html, a pure function).
"Needs your input" and "Waiting for you" are only for a run paused on a question the runner can
resume; a finished run that still has events to review says "Needs review". The function runs
under node."""

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.node_support import node_json

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
BLOCK = re.compile(r"/\* runState:start.*?\*/(.*?)/\* runState:end \*/", re.S)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


def step(key: str, status: str) -> dict:
    return {"key": key, "label": key, "status": status, "summary": None, "tool_calls": []}


def asked(key: str) -> dict:
    """A step paused on a question: it has called ask_caseworker, as the runner records it."""
    return {
        **step(key, "needs_input"),
        "tool_calls": [{"tool": "ask_caseworker", "input": "{}", "output": "{}"}],
    }


def run(status: str, *steps: dict) -> dict:
    return {"id": "run_x", "status": status, "steps": list(steps)}


def evaluate(expression: str):
    match = BLOCK.search(INDEX.read_text(encoding="utf-8"))
    assert match, "runState block not found in web/index.html"
    script = (
        'const RESUMABLE = ["extract", "timeline", "gaps"];\n'
        + match.group(1)
        + f"\nconsole.log(JSON.stringify({expression}));"
    )
    return node_json(script)


def states(runs: list[dict | None], left: dict | None = None) -> list[dict]:
    """`left` is what the caseworker still has to do: { pending, blocking }."""
    return evaluate(f"{json.dumps(runs)}.map(r => runState(r, {json.dumps(left)} || undefined))")


NOTHING_LEFT = {"pending": 0, "blocking": 0}


def test_a_finished_run_with_events_to_review_says_needs_review_not_input() -> None:
    [s] = states([run("needs_review", step("extract", "done"), step("timeline", "done"))])
    assert (s["rail"], s["pill"], s["asking"], s["attn"]) == (
        "Needs review",
        ["warn", "Needs review"],
        False,
        True,
    )


def test_a_seeded_run_whose_gaps_step_needs_input_is_not_a_question() -> None:
    """A seeded run says `needs_input` on its gaps step without ever having asked: there is
    nothing the runner can resume, and an answer would only get a 409."""
    [s] = states([run("needs_review", step("timeline", "done"), step("gaps", "needs_input"))])
    assert (s["rail"], s["pill"][1], s["asking"]) == ("Needs review", "Needs review", False)


@pytest.mark.parametrize("key", ["timeline", "extract", "gaps"])
def test_a_run_paused_on_a_question_says_so(key: str) -> None:
    [s] = states([run("needs_review", asked(key))])
    assert (s["rail"], s["pill"], s["asking"]) == (
        "Needs your input",
        ["warn", "Waiting for you"],
        True,
    )


def test_a_question_that_was_answered_is_not_asked_again() -> None:
    [s] = states([run("needs_review", step("timeline", "done"))])
    assert s["rail"] == "Needs review" and not s["asking"]


def test_done_running_failed_and_no_run() -> None:
    done, running, failed, none = states(
        [
            run("done", step("timeline", "done")),
            run("running", step("timeline", "running")),
            run("failed", step("timeline", "failed")),
            None,
        ]
    )
    assert (done["rail"], done["pill"], done["done"]) == ("Done", ["ok", "Done"], True)
    assert (running["rail"], running["pill"]) == ("Running…", ["info", "Running"])
    assert (failed["rail"], failed["pill"]) == ("Failed", ["bad", "Failed"])
    assert (none["rail"], none["done"], none["attn"]) == ("Not run yet", False, False)


def test_only_a_run_that_needs_review_asks_for_attention() -> None:
    done, review = states([run("done"), run("needs_review", step("timeline", "done"))])
    assert not done["attn"] and review["attn"]


def test_the_page_knows_which_steps_the_runner_builds_and_can_resume() -> None:
    """`BUILT` and `RESUMABLE` in web/index.html follow runner.IMPLEMENTED_STEPS. A step that can
    pause on a question (the gaps agent can call ask_caseworker) must have an answer control; the
    deterministic `rules` step cannot pause, so it is built but not resumable."""
    from app.services.agent import prompts, runner

    page = INDEX.read_text(encoding="utf-8")

    def names(const: str) -> list[str]:
        match = re.search(rf"const {const} = \[(.*?)\];", page)
        assert match, const
        return re.findall(r'"(\w+)"', match.group(1))

    assert names("BUILT") == list(runner.IMPLEMENTED_STEPS)
    # `draft` has no ask_caseworker tool (prompts.DRAFT_TOOLS), so it never pauses either.
    assert "ask_caseworker" not in prompts.DRAFT_TOOLS
    never_pause = ("rules", "draft")
    assert names("RESUMABLE") == [k for k in runner.IMPLEMENTED_STEPS if k not in never_pause]


@pytest.mark.parametrize("key", ["timeline", "extract", "gaps"])
def test_a_resumable_step_that_never_asked_is_not_paused_on_a_question(key: str) -> None:
    [s] = states([run("needs_review", step(key, "needs_input"))])
    assert (s["rail"], s["asking"]) == ("Needs review", False)


def test_a_step_the_runner_cannot_resume_is_never_a_question_even_if_it_asked() -> None:
    [s] = states([run("needs_review", asked("rules"))])
    assert (s["rail"], s["asking"]) == ("Needs review", False)


# --- #119: nothing left to review, and steps that stopped early


def test_a_reviewed_run_with_nothing_left_says_done() -> None:
    [s] = states([run("needs_review", step("timeline", "done"))], NOTHING_LEFT)
    assert (s["rail"], s["pill"], s["done"], s["attn"]) == ("Done", ["ok", "Done"], True, False)


@pytest.mark.parametrize("left", [{"pending": 1, "blocking": 0}, {"pending": 0, "blocking": 2}])
def test_a_run_with_something_left_still_needs_review(left: dict) -> None:
    [s] = states([run("needs_review", step("timeline", "done"))], left)
    assert (s["rail"], s["done"], s["attn"]) == ("Needs review", False, True)


@pytest.mark.parametrize("left", [NOTHING_LEFT, {"pending": 3, "blocking": 1}])
@pytest.mark.parametrize("status", ["needs_review", "done"])
def test_a_step_that_stopped_early_says_incomplete_whatever_else_is_green(
    status: str, left: dict
) -> None:
    [s] = states([run(status, step("timeline", "done"), step("gaps", "incomplete"))], left)
    assert (s["rail"], s["pill"], s["done"], s["attn"]) == (
        "Incomplete",
        ["warn", "Incomplete"],
        False,
        True,
    )


def test_a_failed_run_and_an_open_question_come_before_incomplete() -> None:
    stopped = step("timeline", "incomplete")
    runs: list[dict | None] = [
        run("failed", stopped, step("gaps", "failed")),
        run("needs_review", stopped, asked("gaps")),
    ]
    failed, waiting = states(runs, NOTHING_LEFT)
    assert failed["rail"] == "Failed" and waiting["rail"] == "Needs your input"


def test_open_items_come_from_the_page_counts() -> None:
    left = evaluate("openItems({ total: 5, reviewed: 3, blocking: 1 })")
    assert left == {"pending": 2, "blocking": 1}


def labelled(key: str, label: str) -> dict:
    return {**step(key, "incomplete"), "label": label}


def test_the_page_warns_in_the_same_words_as_the_server() -> None:
    from app.services.package import analysis_warning

    gaps = labelled("gaps", "Find missing information")
    for stopped in ([labelled("timeline", "Build timeline"), gaps], [gaps]):
        shown = evaluate(f"analysisWarning({json.dumps(run('needs_review', *stopped))})")
        assert [shown["lead"], shown["steps"]] == analysis_warning(stopped)
    assert evaluate(f"analysisWarning({json.dumps(run('done', step('gaps', 'done')))})") is None
    assert evaluate("analysisWarning(null)") is None


def test_the_run_and_package_pages_show_the_warning() -> None:
    page = INDEX.read_text(encoding="utf-8")
    assert "${ask}${analysisNote(run)}" in page
    package = page[page.index("function viewPackage()") :]
    assert package.index("${analysisNote(S.run)}") < package.index('data-action="build"')
