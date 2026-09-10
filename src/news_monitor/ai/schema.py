"""Strict schema of the AI answer.

The model can only return an assessment and a Russian draft. It has no field
that could trigger a Telegram publication: the decision path belongs to the
administrator and to the code that checks the stored status.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from news_monitor.normalization.content import normalize_text

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class AIAssessmentError(ValueError):
    """Raised when the AI answer cannot be parsed or validated."""


class AIAssessment(BaseModel):
    """Validated AI assessment of one article."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    importance: int = Field(ge=0, le=100)
    reason: str = Field(min_length=3, max_length=600)
    title_ru: str = Field(min_length=5, max_length=200)
    draft_ru: str = Field(min_length=30, max_length=1500)
    topics: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("reason", "title_ru", "draft_ru", mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, str):
            return normalize_text(value)
        return value

    @field_validator("topics", mode="before")
    @classmethod
    def _normalize_topics(cls, value: Any) -> Any:
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("title_ru")
    @classmethod
    def _title_has_letters(cls, value: str) -> str:
        if not re.search(r"[^\W\d_]", value, re.UNICODE):
            raise ValueError("title_ru must contain letters")
        return value


def parse_ai_assessment(raw: str | dict[str, Any] | None) -> AIAssessment:
    """Parse and validate a raw AI answer.

    Accepts either a decoded object or the raw text of the model answer. Any
    malformed, incomplete or out-of-range answer raises AIAssessmentError so the
    pipeline can stop before a card is created.
    """
    if raw is None:
        raise AIAssessmentError("empty AI answer")

    payload: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise AIAssessmentError("empty AI answer")
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            match = _JSON_OBJECT_RE.search(text)
            if match is None:
                raise AIAssessmentError("AI answer is not valid JSON") from None
            try:
                payload = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise AIAssessmentError("AI answer is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise AIAssessmentError("AI answer is not a JSON object")

    try:
        return AIAssessment.model_validate(payload)
    except ValidationError as exc:
        raise AIAssessmentError(f"AI answer failed validation: {exc.error_count()} errors") from exc
