"""AI schema validation and the OpenRouter client, without any real request."""

from __future__ import annotations

import json

import httpx
import pytest

from news_monitor.ai.client import AIRequestError, OpenRouterAssessor
from news_monitor.ai.prompt import build_messages, build_user_prompt
from news_monitor.ai.schema import AIAssessmentError, parse_ai_assessment
from news_monitor.sources.base import ArticleContent

VALID_PAYLOAD = {
    "importance": 85,
    "reason": "Затрагивает документы иностранцев.",
    "title_ru": "Болгария меняет правила пребывания",
    "draft_ru": "С 15 октября заявления принимают только онлайн. Срок сокращается до 14 дней.",
    "topics": ["legislation"],
}

ARTICLE = ArticleContent(
    url="https://flagman.bg/statia/1",
    title="Нов закон за чужденците",
    body="От 15 октомври 2026 заявленията се подават електронно. Срокът е 14 дни.",
)


def test_valid_json_is_accepted() -> None:
    assessment = parse_ai_assessment(json.dumps(VALID_PAYLOAD))
    assert assessment.importance == 85
    assert assessment.title_ru == "Болгария меняет правила пребывания"


def test_json_wrapped_in_a_code_fence_is_accepted() -> None:
    raw = "```json\n" + json.dumps(VALID_PAYLOAD) + "\n```"
    assert parse_ai_assessment(raw).importance == 85


def test_json_with_surrounding_prose_is_accepted() -> None:
    raw = "Вот результат:\n" + json.dumps(VALID_PAYLOAD) + "\nКонец."
    assert parse_ai_assessment(raw).importance == 85


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "not json at all",
        "{broken json",
        "[1, 2, 3]",
        '"just a string"',
    ],
)
def test_malformed_answers_are_refused(raw) -> None:
    with pytest.raises(AIAssessmentError):
        parse_ai_assessment(raw)


@pytest.mark.parametrize(
    "override",
    [
        {"importance": 101},
        {"importance": -1},
        {"importance": "высокая"},
        {"importance": None},
        {"title_ru": ""},
        {"title_ru": "12345"},
        {"draft_ru": "коротко"},
        {"reason": ""},
        {"draft_ru": "x" * 1600},
    ],
)
def test_out_of_range_or_incomplete_fields_are_refused(override: dict) -> None:
    payload = dict(VALID_PAYLOAD)
    payload.update(override)
    with pytest.raises(AIAssessmentError):
        parse_ai_assessment(json.dumps(payload))


@pytest.mark.parametrize(
    "missing", ["importance", "reason", "title_ru", "draft_ru"]
)
def test_missing_required_fields_are_refused(missing: str) -> None:
    payload = dict(VALID_PAYLOAD)
    payload.pop(missing)
    with pytest.raises(AIAssessmentError):
        parse_ai_assessment(json.dumps(payload))


def test_unknown_fields_are_refused() -> None:
    payload = dict(VALID_PAYLOAD)
    payload["publish_to_channel"] = True
    with pytest.raises(AIAssessmentError):
        parse_ai_assessment(json.dumps(payload))


def test_schema_has_no_publication_field() -> None:
    assessment = parse_ai_assessment(json.dumps(VALID_PAYLOAD))
    fields = set(type(assessment).model_fields)
    assert fields == {"importance", "reason", "title_ru", "draft_ru", "topics"}


def _chat_response(content: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"choices": [{"message": {"role": "assistant", "content": content}}]},
    )


def _assessor(handler, api_key: str = "test-key-value") -> OpenRouterAssessor:
    transport = httpx.MockTransport(handler)
    return OpenRouterAssessor(
        api_key=api_key,
        model="test/model",
        client=httpx.AsyncClient(transport=transport),
    )


async def test_client_returns_a_validated_assessment(source) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return _chat_response(json.dumps(VALID_PAYLOAD))

    assessor = _assessor(handler)
    assessment = await assessor.assess(ARTICLE, source)

    assert assessment.importance == 85
    assert captured["url"].endswith("/chat/completions")
    assert captured["body"]["model"] == "test/model"
    await assessor.aclose()


async def test_client_refuses_invalid_json(source) -> None:
    assessor = _assessor(lambda request: _chat_response("это не JSON"))
    with pytest.raises(AIAssessmentError):
        await assessor.assess(ARTICLE, source)
    await assessor.aclose()


async def test_client_refuses_an_answer_without_choices(source) -> None:
    assessor = _assessor(lambda request: httpx.Response(200, json={"id": "x"}))
    with pytest.raises(AIAssessmentError):
        await assessor.assess(ARTICLE, source)
    await assessor.aclose()


async def test_client_reports_http_errors(source) -> None:
    assessor = _assessor(lambda request: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(AIRequestError):
        await assessor.assess(ARTICLE, source)
    await assessor.aclose()


async def test_api_key_is_only_sent_as_a_header(source) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode("utf-8")
        return _chat_response(json.dumps(VALID_PAYLOAD))

    assessor = _assessor(handler, api_key="secret-key-for-test")
    await assessor.assess(ARTICLE, source)

    assert seen["auth"] == "Bearer secret-key-for-test"
    assert "secret-key-for-test" not in seen["body"]
    await assessor.aclose()


def test_assessor_requires_a_configured_key() -> None:
    with pytest.raises(ValueError):
        OpenRouterAssessor(api_key="", model="test/model")


def test_prompt_forbids_invented_facts_and_full_copies(source) -> None:
    messages = build_messages(ARTICLE, source)
    system = messages[0]["content"]
    assert "Не добавляй числа" in system
    assert "Не копируй полный текст" in system
    assert "администратор" in system

    user = build_user_prompt(ARTICLE, source)
    assert ARTICLE.url in user
    assert ARTICLE.title in user
