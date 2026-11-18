"""Harness for the end-to-end runner evals. A case is data (tests/fixtures/evals/*.json): the
evidence text, and the scripted model replies for each turn. The provider is swappable: the
default replays the script through FixtureProvider (no network); with RUN_LIVE_EVALS=1 and
ANTHROPIC_API_KEY set (and optionally LLM_MODEL), the same case runs against the real model,
where the tests keep only the checks that must hold whatever the model says."""

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.config import Settings
from app.models import CaseRow, EvidencePageRow, RunRow, TimelineEventRow
from app.services.agent import llm, runner
from app.services.agent.evidence_text import SqlTextStore
from tests.agent_support import CASE, turn

EVAL_DIR = Path(__file__).parent / "fixtures" / "evals"
NONCE = "feedfacecafebeef"
RULES_STRENGTHS = {"needs_support", "single_source", "corroborated"}  # strength.py's vocabulary


def live_enabled() -> bool:
    return os.environ.get("RUN_LIVE_EVALS") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))


@dataclass(frozen=True)
class EvalCase:
    name: str
    data: dict[str, Any]

    def pages(self, key: str = "evidence") -> dict[str, dict[str, str]]:
        """evidence id -> locator -> text."""
        return {
            ev: {p["locator"]: p["text"] for p in pages} for ev, pages in self.data[key].items()
        }

    def scripted(self) -> dict[str, llm.LLMResponse]:
        return {key: turn(*blocks) for key, blocks in self.data["turns"].items()}


def load_case(name: str) -> EvalCase:
    path = EVAL_DIR / f"{name}.json"
    return EvalCase(name, json.loads(path.read_text(encoding="utf-8")))


class RecordingProvider:
    """Wraps any provider and keeps every request, so a test can inspect what the model saw."""

    def __init__(self, inner: llm.LLMProvider) -> None:
        self.inner = inner
        self.requests: list[llm.LLMRequest] = []

    async def complete(self, request: llm.LLMRequest) -> llm.LLMResponse:
        self.requests.append(request)
        return await self.inner.complete(request)


def make_provider(mode: str, case: EvalCase) -> RecordingProvider:
    if mode == "live":
        # The same provider the app builds: honours LLM_MODEL and ANTHROPIC_BASE_URL, so a key
        # that only has one model (or an Anthropic-compatible endpoint) works.
        return RecordingProvider(llm.get_provider(Settings(llm_mode="live")))
    return RecordingProvider(llm.FixtureProvider(scripted=case.scripted()))


@dataclass(frozen=True)
class EvalRun:
    run: RunRow
    events: list[TimelineEventRow]  # the events this run added
    provider: RecordingProvider

    @property
    def timeline_calls(self) -> list[dict[str, Any]]:
        return next(s for s in self.run.steps if s["key"] == "timeline")["tool_calls"]


def _event_ids(session: Session) -> set[str]:
    session.expire_all()
    return {e.id for e in session.exec(
        select(TimelineEventRow).where(TimelineEventRow.case_id == CASE))}


def put_pages(session: Session, evidence: dict[str, dict[str, str]]) -> None:
    """Replace the stored text of the given evidence items (what WS1 ingest writes)."""
    for ev_id, pages in evidence.items():
        old = session.exec(select(EvidencePageRow).where(EvidencePageRow.evidence_id == ev_id))
        for row in old:
            session.delete(row)
        for position, (locator, text) in enumerate(pages.items()):
            session.add(EvidencePageRow(evidence_id=ev_id, locator=locator, case_id=CASE,
                                        position=position, text=text))
    session.commit()


def run_eval(
    session: Session, case: EvalCase, provider: RecordingProvider,
    evidence: dict[str, dict[str, str]] | None = None,
) -> EvalRun:
    """Store the evidence text, then run the timeline step end to end with the database text
    store, exactly as the route does."""
    put_pages(session, evidence if evidence is not None else case.pages())
    before = _event_ids(session)
    case_row = session.get(CaseRow, CASE)
    assert case_row is not None
    row = runner.create_run(session, case_row)
    # The timeline step only: these evals are about its prompt and tools. (The extract step
    # sends evidence in wrapped blocks too and has its own tests in test_agent_extract.py.)
    row = asyncio.run(runner.execute_run(
        session, row, provider, SqlTextStore(session), nonce=NONCE, steps=("timeline",)))
    session.expire_all()
    added = [e for e in session.exec(
        select(TimelineEventRow).where(TimelineEventRow.case_id == CASE)) if e.id not in before]
    return EvalRun(row, added, provider)
