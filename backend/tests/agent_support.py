"""Shared helpers for the agent tests: a text store for the seeded demo case, a tool context,
and scripted model responses. Nothing here reaches the network."""

import asyncio
from typing import Any

from sqlmodel import Session

from app.models import CaseRow
from app.services.agent import llm
from app.services.agent.evidence_text import Fact, InMemoryTextStore, Page
from app.services.agent.tools import ToolContext

CASE = "case_demo_001"
NONCE = "feedfacecafebeef"
OTHER_ORG = "org_harbourside"

LEASE = "The term of this lease begins on May 1, 2026. Rent is $1,150 per month."
MSG1 = "[2026-08-15 09:12] Jordan: Hi, there is water leaking from the bathroom ceiling."
MSG2 = "[2026-08-16 18:30] Landlord: I will send someone to look at it this week."
RECEIPT = "Receipt: dehumidifier purchased on 2026-09-25, total $189.99."
CAPTION = "Photo caption: mould on the bathroom ceiling, taken September 2026."


def demo_store() -> InMemoryTextStore:
    store = InMemoryTextStore()
    store.add_pages(
        CASE, "ev_01", [Page("page 1", LEASE), Page("page 2", "Landlord: Harbour Ltd.")]
    )
    store.add_pages(CASE, "ev_04", [Page("message 1", MSG1), Page("message 2", MSG2)])
    store.add_pages(CASE, "ev_06", [Page("photo 1", CAPTION)])
    store.add_pages(CASE, "ev_07", [Page("page 1", RECEIPT)])
    store.add_facts(CASE, [
        Fact("ev_04", "message 1", "Tenant reports water leaking from the bathroom ceiling"),
        Fact("ev_07", "page 1", "Dehumidifier bought for $189.99"),
        Fact("ev_ghost", "page 1", "A fact that points at an evidence id outside the case"),
    ])  # fmt: skip
    return store


def make_ctx(
    session: Session, store: InMemoryTextStore | None = None, *, case_id: str = CASE,
    org_id: str | None = None, nonce: str = NONCE, run_id: str = "run_test",
) -> ToolContext:
    if org_id is None:
        row = session.get(CaseRow, case_id)
        assert row is not None
        org_id = row.org_id
    return ToolContext(
        session=session, org_id=org_id, case_id=case_id, run_id=run_id,
        texts=store or demo_store(), nonce=nonce,
    )  # fmt: skip


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def call(tool_name: str, ctx: ToolContext, raw: dict[str, Any]) -> Any:
    """Validate the raw input like the loop does, then run the tool."""
    from app.services.agent.tools import TOOLS, ToolError, validate_input

    tool = TOOLS[tool_name]
    parsed = validate_input(tool, raw)
    if isinstance(parsed, ToolError):
        return parsed
    return run(tool.run(ctx, parsed))


# --- scripted model responses


def tool_use(name: str, tool_id: str, **inp: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def turn(*blocks: dict[str, Any], stop: str = "tool_use", tokens: tuple[int, int] = (100, 20)
         ) -> llm.LLMResponse:
    uses = [llm.ToolUse(id=b["id"], name=b["name"], input=b["input"])
            for b in blocks if b["type"] == "tool_use"]  # fmt: skip
    text = "".join(b["text"] for b in blocks if b["type"] == "text")
    return llm.LLMResponse(
        content=list(blocks), text=text, tool_uses=uses, stop_reason=stop,  # type: ignore[arg-type]
        usage=llm.Usage(input_tokens=tokens[0], output_tokens=tokens[1]),
    )


def thinking(text: str = "plan") -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": "sig-" + text}


class RecordingProvider(llm.FixtureProvider):
    """A FixtureProvider that also keeps every request it was asked to answer."""

    def __init__(self, scripted: dict[str, llm.LLMResponse]) -> None:
        super().__init__(scripted=scripted)
        self.requests: list[llm.LLMRequest] = []

    async def complete(self, request: llm.LLMRequest) -> llm.LLMResponse:
        self.requests.append(request)
        return await super().complete(request)


def blocks(message: llm.Message) -> list[dict[str, Any]]:
    """The content blocks of a message that is known to carry blocks, not a plain string."""
    assert isinstance(message.content, list)
    return message.content
