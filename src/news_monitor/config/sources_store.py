"""Validated and atomic editing of ``config/sources.yaml``.

Every change follows the same order:

1. the current file is read and validated, so a broken file is never edited;
2. the change is applied to an in-memory copy of the document;
3. the whole document is validated again by the Pydantic model;
4. only then the file is replaced atomically.

A rejected change therefore leaves the file byte-for-byte untouched. The store
owns exactly one path (``Settings.sources_file``) and never writes anywhere else.
No secret is read or written here: ``sources.yaml`` holds no credentials.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError

from news_monitor.config.files import atomic_write_text
from news_monitor.config.sources import SourceConfig, SourcesFile
from news_monitor.normalization.url import normalize_url, same_host

logger = logging.getLogger(__name__)

TopicKind = Literal["inclusion", "exclusion"]

MIN_INTERVAL_MINUTES = 1
MAX_INTERVAL_MINUTES = 1440
MAX_TEXT_LENGTH = 120
MAX_URL_LENGTH = 400
MAX_TOPICS_PER_KIND = 40
MAX_SECTIONS = 20

_TOPIC_FIELDS: dict[str, str] = {
    "inclusion": "inclusion_rules",
    "exclusion": "exclusion_rules",
}

_TRUE_WORDS = frozenset({"on", "true", "1", "yes", "вкл", "да"})
_FALSE_WORDS = frozenset({"off", "false", "0", "no", "выкл", "нет"})


class _IndentedDumper(yaml.SafeDumper):
    """Dumper that indents list items under their key, as the file is written."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


class SourcesStoreError(ValueError):
    """A rejected edit. The message is safe to show to the administrator."""


@dataclass(frozen=True, slots=True)
class EditResult:
    """The saved source plus a short description of what changed."""

    source: SourceConfig
    description: str


def _clean_text(value: str, *, field: str) -> str:
    """Return a single-line, length-limited value or raise."""
    text = " ".join(str(value or "").split())
    if not text:
        raise SourcesStoreError(f"Значение «{field}» не может быть пустым.")
    if len(text) > MAX_TEXT_LENGTH:
        raise SourcesStoreError(f"Значение «{field}» длиннее {MAX_TEXT_LENGTH} символов.")
    return text


def parse_interval(value: str | int) -> int:
    """Parse and range-check an interval in minutes."""
    try:
        minutes = int(str(value).strip())
    except (TypeError, ValueError):
        raise SourcesStoreError("Интервал должен быть целым числом минут.") from None
    if not MIN_INTERVAL_MINUTES <= minutes <= MAX_INTERVAL_MINUTES:
        raise SourcesStoreError(
            f"Интервал должен быть от {MIN_INTERVAL_MINUTES} "
            f"до {MAX_INTERVAL_MINUTES} минут."
        )
    return minutes


def parse_flag(value: str | bool) -> bool:
    """Parse an on/off flag written by the administrator."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise SourcesStoreError("Ожидается значение on или off.")


def parse_position(value: str | int, *, total: int) -> int:
    """Convert a 1-based position shown to the administrator into an index."""
    try:
        position = int(str(value).strip())
    except (TypeError, ValueError):
        raise SourcesStoreError("Ожидается номер позиции в списке.") from None
    if total <= 0:
        raise SourcesStoreError("Список пуст, удалять нечего.")
    if not 1 <= position <= total:
        raise SourcesStoreError(f"Номер должен быть от 1 до {total}.")
    return position - 1


def topic_field(kind: str) -> str:
    """Return the YAML field holding the topics of ``kind``."""
    try:
        return _TOPIC_FIELDS[kind]
    except KeyError:
        raise SourcesStoreError("Неизвестный тип тем.") from None


def _leading_comments(text: str) -> str:
    """Return the comment block at the top of the file, preserved on rewrite."""
    header: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            header.append(line.rstrip())
            continue
        if not stripped and header:
            continue
        break
    if not header:
        return ""
    return "\n".join(header) + "\n"


class SourcesStore:
    """Reads and edits one ``sources.yaml`` file."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The only file this store may write."""
        return self._path

    # ------------------------------------------------------------------ read

    def load(self) -> list[SourceConfig]:
        """Return every configured source."""
        _, document = self._read_document()
        return _validate(document)

    def get(self, source_id: str) -> SourceConfig:
        """Return one source by id."""
        wanted = str(source_id or "").strip()
        for source in self.load():
            if source.id == wanted:
                return source
        raise SourcesStoreError(f"Источник не найден: {wanted or '-'}")

    # ----------------------------------------------------------------- edits

    def set_enabled(self, source_id: str, enabled: bool) -> EditResult:
        """Turn a source on or off."""

        def mutate(entry: dict) -> str:
            entry["enabled"] = bool(enabled)
            return "источник включён" if enabled else "источник выключен"

        return self._edit(source_id, mutate)

    def set_interval(self, source_id: str, minutes: int | str) -> EditResult:
        """Set the minimal polling interval of a source."""
        value = parse_interval(minutes)

        def mutate(entry: dict) -> str:
            entry["min_interval_minutes"] = value
            return f"интервал: {value} мин"

        return self._edit(source_id, mutate)

    def add_section(self, source_id: str, name: str, url: str) -> EditResult:
        """Add a listing page belonging to the host of the source."""
        clean_name = _clean_text(name, field="название раздела")

        def mutate(entry: dict) -> str:
            sections = entry.setdefault("sections", [])
            if not isinstance(sections, list):
                raise SourcesStoreError("Список разделов повреждён.")
            if len(sections) >= MAX_SECTIONS:
                raise SourcesStoreError(f"Больше {MAX_SECTIONS} разделов нельзя.")
            clean_url = _section_url(url, base_url=str(entry.get("base_url", "")))
            for section in sections:
                if not isinstance(section, dict):
                    continue
                if _same_url(str(section.get("url", "")), clean_url):
                    raise SourcesStoreError("Такой раздел уже есть.")
                if str(section.get("name", "")).casefold() == clean_name.casefold():
                    raise SourcesStoreError("Раздел с таким названием уже есть.")
            sections.append({"name": clean_name, "url": clean_url})
            return f"добавлен раздел: {clean_name}"

        return self._edit(source_id, mutate)

    def remove_section(self, source_id: str, index: int) -> EditResult:
        """Remove a listing page by its zero-based index."""

        def mutate(entry: dict) -> str:
            sections = entry.get("sections")
            if not isinstance(sections, list) or not 0 <= index < len(sections):
                raise SourcesStoreError("Раздел не найден.")
            if len(sections) <= 1:
                raise SourcesStoreError("Нельзя удалить последний раздел источника.")
            removed = sections.pop(index)
            name = removed.get("name") if isinstance(removed, dict) else removed
            return f"удалён раздел: {name}"

        return self._edit(source_id, mutate)

    def add_topic(self, source_id: str, kind: TopicKind, topic: str) -> EditResult:
        """Add an inclusion or exclusion topic."""
        field = topic_field(kind)
        clean_topic = _clean_text(topic, field="тема")

        def mutate(entry: dict) -> str:
            topics = _topics_list(entry, field)
            if len(topics) >= MAX_TOPICS_PER_KIND:
                raise SourcesStoreError(f"Больше {MAX_TOPICS_PER_KIND} тем нельзя.")
            if any(str(item).casefold() == clean_topic.casefold() for item in topics):
                raise SourcesStoreError("Такая тема уже есть.")
            topics.append(clean_topic)
            return f"добавлена тема: {clean_topic}"

        return self._edit(source_id, mutate)

    def remove_topic(self, source_id: str, kind: TopicKind, index: int) -> EditResult:
        """Remove an inclusion or exclusion topic by its zero-based index."""
        field = topic_field(kind)

        def mutate(entry: dict) -> str:
            topics = _topics_list(entry, field)
            if not 0 <= index < len(topics):
                raise SourcesStoreError("Тема не найдена.")
            removed = topics.pop(index)
            return f"удалена тема: {removed}"

        return self._edit(source_id, mutate)

    # --------------------------------------------------------------- private

    def _edit(self, source_id: str, mutate: Callable[[dict], str]) -> EditResult:
        header, document = self._read_document()
        wanted = str(source_id or "").strip()
        entry = _entry_of(document, wanted)
        description = mutate(entry)
        for source in self._write(header, document):
            if source.id == wanted:
                return EditResult(source=source, description=description)
        raise SourcesStoreError("Источник исчез из файла во время изменения.")

    def _read_document(self) -> tuple[str, dict]:
        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise SourcesStoreError("Файл источников не найден.") from None
        except OSError:
            raise SourcesStoreError("Файл источников недоступен для чтения.") from None
        try:
            data = yaml.safe_load(raw_text)
        except yaml.YAMLError:
            raise SourcesStoreError("Файл источников содержит некорректный YAML.") from None
        if data is None:
            data = {"sources": []}
        if not isinstance(data, dict):
            raise SourcesStoreError("Файл источников имеет неожиданную структуру.")
        _validate(data)
        return _leading_comments(raw_text), data

    def _write(self, header: str, document: dict) -> list[SourceConfig]:
        """Validate the whole document, then replace the file atomically."""
        sources = _validate(document)
        body = yaml.dump(
            document,
            Dumper=_IndentedDumper,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=1000,
        )
        self._atomic_write(header + body)
        return sources

    def _atomic_write(self, text: str) -> None:
        try:
            atomic_write_text(self._path, text)
        except OSError:
            logger.exception("cannot save the sources file")
            raise SourcesStoreError("Не удалось сохранить файл источников.") from None


def _validate(document: dict) -> list[SourceConfig]:
    try:
        return SourcesFile.model_validate(document).sources
    except ValidationError as exc:
        raise SourcesStoreError(_readable_error(exc)) from None


def _readable_error(exc: ValidationError) -> str:
    """Turn a validation error into a short message without file paths."""
    problems: list[str] = []
    for error in exc.errors()[:3]:
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = str(error.get("msg", "некорректное значение"))
        problems.append(f"{location}: {message}" if location else message)
    details = "; ".join(problems) or "некорректное значение"
    return f"Конфигурация не прошла проверку ({details}). Файл не изменён."


def _entry_of(document: dict, source_id: str) -> dict:
    entries = document.get("sources")
    if not isinstance(entries, list):
        raise SourcesStoreError("Файл источников имеет неожиданную структуру.")
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("id", "")).strip() == source_id:
            return entry
    raise SourcesStoreError(f"Источник не найден: {source_id or '-'}")


def _topics_list(entry: dict, field: str) -> list:
    rules = entry.setdefault(field, {})
    if not isinstance(rules, dict):
        raise SourcesStoreError("Список тем повреждён.")
    topics = rules.setdefault("topics", [])
    if not isinstance(topics, list):
        raise SourcesStoreError("Список тем повреждён.")
    return topics


def _same_url(left: str, right: str) -> bool:
    return left.strip().rstrip("/").casefold() == right.strip().rstrip("/").casefold()


def _section_url(url: str, *, base_url: str) -> str:
    """Validate a listing url and keep it on the host of the source."""
    candidate = str(url or "").strip()
    if not candidate:
        raise SourcesStoreError("Адрес раздела не может быть пустым.")
    if len(candidate) > MAX_URL_LENGTH:
        raise SourcesStoreError("Адрес раздела слишком длинный.")
    try:
        normalized = normalize_url(candidate)
    except ValueError:
        raise SourcesStoreError("Адрес раздела должен быть http(s) ссылкой.") from None
    if not urlsplit(normalized).hostname:
        raise SourcesStoreError("Адрес раздела должен содержать домен.")
    if base_url and not same_host(normalized, base_url):
        raise SourcesStoreError("Адрес раздела должен быть на домене источника.")
    return normalized
