"""Prompt construction for the assessment call."""

from __future__ import annotations

import json

from news_monitor.config.sources import SourceConfig
from news_monitor.normalization.content import normalize_text
from news_monitor.sources.base import ArticleContent

MAX_BODY_CHARS = 6000

SYSTEM_PROMPT = """Ты — редактор новостной ленты о Болгарии для русскоязычных читателей.

Правила:
1. Используй только факты из переданного текста. Не добавляй числа, даты, имена и события, которых там нет.
2. Пересказывай своими словами. Не копируй полный текст статьи и не приводи длинные цитаты.
3. Если фактов недостаточно или новость незначима, снижай оценку важности.
4. Отвечай строго одним JSON-объектом без пояснений и без разметки.
5. Ты не публикуешь новости: решение о публикации принимает администратор.

Формат ответа:
{"importance": 0-100, "reason": "...", "title_ru": "...", "draft_ru": "...", "topics": ["..."]}

Где draft_ru — краткий пересказ на русском языке из 2-4 предложений."""


def _topic_line(label: str, topics: list[str]) -> str:
    if not topics:
        return ""
    return f"{label}: {', '.join(topics)}\n"


def build_user_prompt(article: ArticleContent, source: SourceConfig) -> str:
    """Build the user message describing one article and the source rules."""
    body = normalize_text(article.body)[:MAX_BODY_CHARS]
    published = (
        article.published_at.isoformat() if article.published_at else "неизвестно"
    )
    parts = [
        f"Источник: {source.name}\n",
        f"Ссылка: {article.url}\n",
        f"Время публикации: {published}\n",
        _topic_line("Интересные темы", source.inclusion_rules.topics),
        _topic_line("Нежелательные темы", source.exclusion_rules.topics),
        f"\nЗаголовок оригинала: {normalize_text(article.title)}\n",
        f"\nТекст статьи:\n{body}\n",
        "\nОцени важность новости для русскоязычной аудитории в Болгарии ",
        "и подготовь черновик по правилам выше. Верни только JSON.",
    ]
    return "".join(parts)


def build_messages(article: ArticleContent, source: SourceConfig) -> list[dict[str, str]]:
    """Build the chat messages sent to the model."""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(article, source)},
    ]


RESPONSE_JSON_SCHEMA: dict[str, object] = {
    "name": "news_assessment",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["importance", "reason", "title_ru", "draft_ru", "topics"],
        "properties": {
            "importance": {"type": "integer", "minimum": 0, "maximum": 100},
            "reason": {"type": "string"},
            "title_ru": {"type": "string"},
            "draft_ru": {"type": "string"},
            "topics": {"type": "array", "items": {"type": "string"}},
        },
    },
}


def response_format() -> dict[str, object]:
    """Return the OpenRouter response_format payload requesting strict JSON."""
    return {"type": "json_schema", "json_schema": json.loads(json.dumps(RESPONSE_JSON_SCHEMA))}
