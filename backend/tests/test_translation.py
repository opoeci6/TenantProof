"""Issue #96: the caseworker's reading aid for a quote in another language.

A quote is translated once and cached by language and text; the original is what the rest of the
app cites and files. Scripted FixtureProvider only.
"""

from collections.abc import Iterator

import pytest
from sqlmodel import select

from app.main import api
from app.models import ActivityRow, ExcerptTranslationRow
from app.routers import translation as translation_router
from app.services import excerpt_translation
from app.services.agent import llm, prompts
from tests.agent_support import CASE, RecordingProvider

ZH = "房东说下周一之前会来修。"
EN = "The landlord said he will come to fix it before next Monday."
PATH = f"/api/cases/{CASE}/translate-excerpt"


class Provider(RecordingProvider):
    """Answers the translation with `reply`, or fails with `error`."""

    def __init__(
        self, reply: str = EN, *, stop: str = "end_turn", error: Exception | None = None
    ) -> None:
        super().__init__(
            scripted={
                prompts.TRANSLATE_EXCERPT_FIXTURE_KEY: llm.LLMResponse(
                    text=reply,
                    stop_reason=stop,  # type: ignore[arg-type]
                    content=[{"type": "text", "text": reply}],
                )
            }
        )
        self.error = error

    async def complete(self, request: llm.LLMRequest) -> llm.LLMResponse:
        if self.error is not None:
            self.requests.append(request)
            raise self.error
        return await super().complete(request)


@pytest.fixture
def provider() -> Iterator[list[Provider]]:
    current = [Provider()]
    api.dependency_overrides[translation_router.translate_provider] = lambda: current[-1]
    yield current
    api.dependency_overrides.pop(translation_router.translate_provider, None)


def _translate(client, excerpt: str = ZH, language: str = "zh"):
    return client.post(PATH, json={"excerpt": excerpt, "language": language})


def test_a_quote_is_translated_once_and_the_second_request_is_free(
    fresh_db, client, provider, session
) -> None:
    first = _translate(client)
    assert first.status_code == 200 and first.json() == {"text_en": EN}
    assert len(provider[0].requests) == 1

    second = _translate(client)
    assert second.status_code == 200 and second.json() == {"text_en": EN}
    assert len(provider[0].requests) == 1  # cache hit: no second model call

    session.expire_all()
    row = session.get(ExcerptTranslationRow, excerpt_translation.key_for("zh", ZH))
    assert row is not None and row.text_en == EN


@pytest.mark.parametrize("quote", ["The landlord said he will fix it.", "café"])
def test_an_english_quote_needs_no_model_call(fresh_db, client, provider, quote: str) -> None:
    r = _translate(client, quote, "en")
    assert r.status_code == 200
    assert r.json() == {"text_en": quote}
    assert provider[0].requests == []


def test_an_ascii_french_quote_is_translated_and_cached(
    fresh_db, client, provider, session
) -> None:
    fr_ascii = "Notre plombier vous contactera avant le 5 septembre."
    fr_en = "Our plumber will contact you before September 5."
    provider.append(Provider(fr_en))

    first = _translate(client, fr_ascii, "fr")
    assert first.status_code == 200 and first.json() == {"text_en": fr_en}
    assert len(provider[-1].requests) == 1

    second = _translate(client, fr_ascii, "fr")
    assert second.status_code == 200 and second.json() == {"text_en": fr_en}
    assert len(provider[-1].requests) == 1

    session.expire_all()
    row = session.get(ExcerptTranslationRow, excerpt_translation.key_for("fr", fr_ascii))
    assert row is not None and row.text_en == fr_en


def test_translate_excerpt_prompt_keeps_english_verbatim() -> None:
    assert prompts.TRANSLATE_EXCERPT_PROMPT_VERSION == "translate-excerpt-v2"
    assert "already in English" in prompts.translate_excerpt_system()


def test_the_quote_goes_to_the_model_inside_an_evidence_block(fresh_db, client, provider) -> None:
    _translate(client)
    request = provider[0].requests[0]
    content = request.messages[0].content
    assert isinstance(content, str)
    assert "<evidence-" in content and "</evidence-" in content and ZH in content
    assert request.system == prompts.translate_excerpt_system()


def test_another_orgs_case_is_404(fresh_db, other_org_client, provider) -> None:
    r = other_org_client.post(PATH, json={"excerpt": ZH, "language": "zh"})
    assert r.status_code == 404
    assert provider[0].requests == []


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"excerpt": "长" * 3001}, "1 to 3000"),
        ({"excerpt": ""}, "1 to 3000"),
        ({"excerpt": ZH, "language": "klingon"}, "Unknown language"),
        ({"excerpt": ZH, "unexpected": True}, "1 to 3000"),
    ],
)
def test_a_bad_request_is_refused_with_a_plain_reason(
    fresh_db, client, provider, body, expected
) -> None:
    r = client.post(PATH, json=body)
    assert r.status_code == 422
    assert expected in r.json()["detail"]["reasons"][0]
    assert provider[0].requests == []


@pytest.mark.parametrize(
    "failing",
    [
        Provider(error=llm.LLMTimeout("slow")),
        Provider(error=llm.FixtureMissing("none")),
        Provider("", stop="end_turn"),
        Provider(EN, stop="max_tokens"),
    ],
)
def test_a_failed_model_call_is_503_and_saves_nothing(
    fresh_db, client, provider, session, failing
) -> None:
    provider.append(failing)
    r = _translate(client)
    assert r.status_code == 503
    assert r.json()["detail"]["reasons"] == [excerpt_translation.NO_MODEL]
    session.expire_all()
    assert session.get(ExcerptTranslationRow, excerpt_translation.key_for("zh", ZH)) is None


def test_the_translation_is_logged(fresh_db, client, provider, session) -> None:
    assert _translate(client).status_code == 200
    session.expire_all()
    actions = [
        row.action
        for row in session.exec(select(ActivityRow).where(ActivityRow.case_id == CASE)).all()
    ]
    assert "Translated a quoted source" in actions


def test_with_placeholders_on_a_translation_is_cached_for_its_case_only(
    fresh_db, client, provider, session, monkeypatch
) -> None:
    """Issue #60: the reply is revealed with this case's details, so another case (or org) asking
    for the same words must not be served it from the cache."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "privacy_placeholders", True)
    assert _translate(client).status_code == 200
    session.expire_all()
    assert session.get(ExcerptTranslationRow, excerpt_translation.key_for("zh", ZH)) is None
    assert session.get(ExcerptTranslationRow, excerpt_translation.key_for("zh", ZH, CASE))
    assert excerpt_translation.cached(session, "zh", ZH, "case_other") is None
