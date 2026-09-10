"""OpenRouter HTTP client returning a Pydantic validated assessment."""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import httpx

from news_monitor.ai.prompt import build_messages, response_format
from news_monitor.ai.schema import AIAssessment, AIAssessmentError, parse_ai_assessment
from news_monitor.config.sources import SourceConfig
from news_monitor.sources.base import ArticleContent

logger = logging.getLogger(__name__)


class AIRequestError(RuntimeError):
    """Raised when the assessment request itself fails."""


@runtime_checkable
class AIAssessor(Protocol):
    """Produces a validated assessment for one article."""

    async def assess(
        self, article: ArticleContent, source: SourceConfig
    ) -> AIAssessment: ...


class OpenRouterAssessor:
    """Calls the OpenRouter chat completions endpoint.

    The API key is read from settings at call time and only used as a request
    header. It is never logged, stored or included in an exception message.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
        temperature: float = 0.2,
    ) -> None:
        if not api_key:
            raise ValueError("OpenRouter API key is not configured")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self._temperature = temperature

    async def __aenter__(self) -> "OpenRouterAssessor":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def build_payload(
        self, article: ArticleContent, source: SourceConfig
    ) -> dict[str, Any]:
        """Build the chat completion request body."""
        return {
            "model": self._model,
            "messages": build_messages(article, source),
            "temperature": self._temperature,
            "response_format": response_format(),
        }

    async def aclose(self) -> None:
        """Close the HTTP client when this assessor created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def assess(
        self, article: ArticleContent, source: SourceConfig
    ) -> AIAssessment:
        """Return a validated assessment or raise on any invalid answer."""
        client = self._get_client()
        url = f"{self._base_url}/chat/completions"
        try:
            response = await client.post(
                url,
                headers=self._headers(),
                json=self.build_payload(article, source),
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AIRequestError(
                f"OpenRouter returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AIRequestError(f"OpenRouter request failed: {type(exc).__name__}") from exc

        return parse_ai_assessment(extract_message_content(response))


def extract_message_content(response: httpx.Response) -> str:
    """Extract the assistant message text from an OpenAI compatible answer."""
    try:
        data = response.json()
    except ValueError as exc:
        raise AIAssessmentError("OpenRouter answer is not JSON") from exc
    if not isinstance(data, dict):
        raise AIAssessmentError("OpenRouter answer is not a JSON object")
    if "error" in data:
        raise AIRequestError("OpenRouter reported an error for this request")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIAssessmentError("OpenRouter answer has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise AIAssessmentError("OpenRouter answer has no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise AIAssessmentError("OpenRouter answer has empty content")
    return content
