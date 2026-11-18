"""est_cost_usd uses the price of the model actually used (LLM_MODEL), from a list-price table.
A model that is not listed, or a Haiku 5.5 prompt long enough for its second rate card, gets no
estimate (None, shown as "unknown"), never another model's number."""

import asyncio

import pytest
from sqlmodel import Session

from app.config import Settings
from app.models import CaseRow
from app.schemas import RunUsage
from app.services.agent import llm, runner
from tests.agent_support import CASE, RecordingProvider, demo_store
from tests.test_agent_runner import execute_timeline, full_script

M = 1_000_000
N = 100_000  # tokens: the most a Haiku 5.5 prompt can have and still be priced


def usage(**kw: int) -> llm.Usage:
    return llm.Usage(**kw)


# --------------------------------------------------------------------------- the table


@pytest.mark.parametrize(("model", "in_price", "out_price"), [
    ("claude-haiku-5-5", 0.10, 0.50),
    ("claude-sonnet-5-5", 2.0, 10.0),
    ("claude-opus-5-5", 4.0, 20.0),
    ("claude-fable-5-2", 8.0, 40.0),
])
def test_input_and_output_use_the_models_own_price(model: str, in_price: float,
                                                    out_price: float) -> None:
    assert llm.estimate_cost_usd(usage(input_tokens=N), model) == round(in_price / 10, 4)
    assert llm.estimate_cost_usd(usage(output_tokens=M), model) == out_price


def test_haiku_5_5_is_a_fifth_of_the_default_model_not_the_same() -> None:
    work = usage(input_tokens=24_996, output_tokens=6_145)
    haiku = llm.estimate_cost_usd(work, "claude-haiku-5-5")
    sonnet = llm.estimate_cost_usd(work, llm.MODEL)
    assert haiku == round(24_996 * 0.10 / M + 6_145 * 0.50 / M, 4) == 0.0056
    assert sonnet == round(24_996 * 2.0 / M + 6_145 * 10.0 / M, 4) == 0.1114
    assert haiku is not None and sonnet is not None and haiku < sonnet / 15


@pytest.mark.parametrize(("model", "read_price"), [
    ("claude-haiku-5-5", 0.01), ("claude-sonnet-5-5", 0.10),
    ("claude-opus-5-5", 0.20), ("claude-fable-5-2", 0.20),
])
def test_cache_reads_use_the_models_read_price_and_writes_cost_1_25_times_input(
    model: str, read_price: float
) -> None:
    price = llm.PRICES[model]
    assert llm.estimate_cost_usd(usage(cache_read_input_tokens=N), model) == round(
        read_price / 10, 4)
    assert llm.estimate_cost_usd(usage(cache_creation_input_tokens=N), model) == round(
        price.in_per_mtok * 1.25 / 10, 4)


def test_the_default_model_is_in_the_table() -> None:
    assert llm.MODEL in llm.PRICES


# --------------------------------------------------------------------------- unknown


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-opus-4-8", "gpt-5", "", "x"])
def test_an_unlisted_model_has_no_estimate(model: str) -> None:
    assert llm.estimate_cost_usd(usage(input_tokens=M, output_tokens=M), model) is None


def test_zero_usage_of_an_unlisted_model_is_still_unknown_not_zero() -> None:
    assert llm.estimate_cost_usd(usage(), "claude-haiku-4-5") is None
    assert llm.estimate_cost_usd(usage(), llm.MODEL) == 0.0


def test_a_haiku_prompt_over_100k_tokens_has_no_estimate() -> None:
    """Above 100K prompt tokens Haiku 5.5 has a dearer rate card this table does not carry."""
    assert llm.estimate_cost_usd(usage(input_tokens=100_000), "claude-haiku-5-5") is not None
    assert llm.estimate_cost_usd(usage(input_tokens=100_001), "claude-haiku-5-5") is None
    # cache tokens are part of the prompt
    assert llm.estimate_cost_usd(
        usage(input_tokens=1, cache_read_input_tokens=100_000), "claude-haiku-5-5") is None
    assert llm.estimate_cost_usd(usage(input_tokens=500_000), llm.MODEL) is not None


def test_many_short_prompts_are_not_one_long_prompt() -> None:
    """Three 60K requests total 180K, but none is over 100K, so Haiku is still priced."""
    one = usage(input_tokens=60_000, output_tokens=100)
    total = one + one + one
    assert total.input_tokens == 180_000 and total.peak_prompt_tokens == 60_000
    assert llm.estimate_cost_usd(total, "claude-haiku-5-5") is not None
    big = one + usage(input_tokens=120_000)
    assert big.peak_prompt_tokens == 120_000
    assert llm.estimate_cost_usd(big, "claude-haiku-5-5") is None


def test_the_peak_survives_a_save_and_load() -> None:
    saved = (usage(input_tokens=120_000) + usage(input_tokens=10)).model_dump()
    assert llm.Usage.model_validate(saved).peak_prompt_tokens == 120_000


# --------------------------------------------------------------------------- the run


class ModelProvider(RecordingProvider):
    """A scripted provider that says which model it calls, as AnthropicProvider does."""

    def __init__(self, script: dict, model: str) -> None:
        super().__init__(script)
        self.model = model


def finished(session: Session, model: str) -> dict:
    case = session.get(CaseRow, CASE)
    assert case is not None
    row = asyncio.run(execute_timeline(
        session, runner.create_run(session, case), ModelProvider(full_script(), model),
        demo_store()))
    assert row.usage is not None
    return row.usage


def test_a_run_is_priced_with_the_model_its_provider_used(
    fresh_db: None, hearing_known: None, session: Session
) -> None:
    haiku = finished(session, "claude-haiku-5-5")
    sonnet = finished(session, "claude-sonnet-5-5")
    assert haiku["model"] == "claude-haiku-5-5" and sonnet["model"] == "claude-sonnet-5-5"
    # 400 input + 80 output tokens in the scripted run
    assert haiku["est_cost_usd"] == round(400 * 0.10 / M + 80 * 0.50 / M, 4)
    assert sonnet["est_cost_usd"] == round(400 * 2.0 / M + 80 * 10.0 / M, 4)


def test_a_run_on_an_unlisted_model_stores_null_not_a_wrong_number(
    fresh_db: None, hearing_known: None, session: Session
) -> None:
    used = finished(session, "claude-haiku-4-5")
    assert used["est_cost_usd"] is None and used["model"] == "claude-haiku-4-5"
    assert used["input_tokens"] == 400  # the token counts are still there


def test_a_provider_without_a_model_is_the_default_model() -> None:
    assert llm.model_name(llm.FixtureProvider(scripted={})) == llm.MODEL
    assert llm.model_name(object()) == llm.MODEL


def test_the_live_provider_reports_llm_model() -> None:
    provider = llm.AnthropicProvider(api_key="k", model="claude-haiku-5-5", client=object())
    assert llm.model_name(provider) == "claude-haiku-5-5"
    settings = Settings(llm_mode="live", llm_model="claude-haiku-5-5")
    # get_provider reads the key from the environment; the model name is what matters here
    assert (settings.llm_model or llm.MODEL) == "claude-haiku-5-5"


def test_the_api_accepts_and_returns_a_null_cost(
    fresh_db: None, hearing_known: None, session: Session, client
) -> None:
    case = session.get(CaseRow, CASE)
    assert case is not None
    row = asyncio.run(execute_timeline(
        session, runner.create_run(session, case),
        ModelProvider(full_script(), "claude-haiku-4-5"), demo_store()))
    body = client.get(f"/api/runs/{row.id}").json()
    assert body["usage"]["est_cost_usd"] is None and body["usage"]["model"] == "claude-haiku-4-5"
    assert RunUsage.model_validate(body["usage"]).est_cost_usd is None
