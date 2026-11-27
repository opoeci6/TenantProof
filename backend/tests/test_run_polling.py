"""Run-page reload and case-switch regression tests with a controlled timer."""

import json
import re
import shutil
from pathlib import Path

import pytest

from tests.node_support import node_json

INDEX = Path(__file__).resolve().parents[2] / "web" / "index.html"
BLOCK = re.compile(r"/\* runPolling:start.*?\*/(.*?)/\* runPolling:end \*/", re.S)
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

HARNESS = """
let CASE_ID = "case-a", TENANT_MODE = false, CASELESS_LOADERS = [];
const S = { run: null }, events = [], timers = new Map();
let seq = 0, resolveResponse, calls = 0, view = {};
function setInterval(tick) { timers.set(++seq, tick); return seq; }
function clearInterval(id) { timers.delete(id); }
const LOADERS = { run: async () => { S.run = {id: "run-a", status: "running"}; } };
const render = () => events.push("render");
const renderChrome = () => {};
const route = () => "run";
const $ = key => key === "#ask-a" ? {focus: () => events.push("focus")} : view;
const viewRun = () => "";
const askStep = run => run.question || null;
const toast = msg => events.push(msg);
const refresh = async () => { render(); };
const request = (method, path) => {
  if (method !== "GET" || path !== "/runs/run-a") throw Error(path);
  calls++;
  return new Promise(resolve => { resolveResponse = resolve; });
};
"""


def exercise(script: str) -> dict:
    match = BLOCK.search(INDEX.read_text(encoding="utf-8"))
    assert match
    return node_json(HARNESS + match.group(1) + "\n(async () => {" + script + "})();")


@pytest.mark.parametrize("question", [True, False])
def test_reload_polls_until_the_run_pauses_or_finishes(question: bool) -> None:
    info = exercise(f"""
      await loadAndRender();
      const tick = timers.values().next().value;
      const response = tick();
      await tick();
      resolveResponse({{id: "run-a", status: "needs_review", question: {json.dumps(question)}}});
      await response;
      console.log(JSON.stringify({{calls, timers: timers.size, status: S.run.status, events}}));
    """)
    assert (info["calls"], info["timers"], info["status"]) == (1, 0, "needs_review")
    expected = "The agent has a question for you." if question else (
        "Analysis finished. Required items need your input."
    )
    assert expected in info["events"]
    assert ("focus" in info["events"]) is question


def test_in_flight_poll_cannot_overwrite_the_next_case() -> None:
    info = exercise("""
      await loadAndRender();
      const response = timers.values().next().value();
      stopRunPolling();
      CASE_ID = "case-b";
      S.run = {id: "run-b", status: "running"};
      pollRun();
      resolveResponse({id: "run-a", status: "needs_review", question: true});
      await response;
      console.log(JSON.stringify({id: S.run.id, timers: timers.size, events}));
    """)
    assert info == {"id": "run-b", "timers": 1, "events": ["render"]}
