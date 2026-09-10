"""Code-enforced quality guards applied to the AI draft.

The rules in this module are deterministic and never delegated to the model:

* a draft must not reproduce a long verbatim run from the source article;
* every multi-digit number in the draft must be supported by the source text;
* the source link must be present before anything can be published.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from difflib import SequenceMatcher

from news_monitor.normalization.content import normalize_for_hash, normalize_text

MAX_VERBATIM_WORDS = 20
MAX_DRAFT_TO_SOURCE_RATIO = 0.7
MIN_NUMBER_LENGTH = 2

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_DIGIT_GROUP_RE = re.compile(r"(?<=\d)[\s\u00a0\u202f,.](?=\d{3}\b)")
_NUMBER_RE = re.compile(r"\d+")


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """Outcome of a deterministic draft guard."""

    passed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.passed


PASSED = GuardVerdict(passed=True)


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(normalize_for_hash(text))


def longest_verbatim_run(draft: str, source_text: str) -> int:
    """Return the length, in words, of the longest run shared by both texts."""
    draft_words = _words(draft)
    source_words = _words(source_text)
    if not draft_words or not source_words:
        return 0
    matcher = SequenceMatcher(None, draft_words, source_words, autojunk=False)
    match = matcher.find_longest_match(0, len(draft_words), 0, len(source_words))
    return match.size


def check_not_verbatim_copy(
    draft: str,
    source_text: str,
    *,
    max_words: int = MAX_VERBATIM_WORDS,
    max_ratio: float = MAX_DRAFT_TO_SOURCE_RATIO,
) -> GuardVerdict:
    """Reject a draft that copies the source article instead of summarising it."""
    draft_words = _words(draft)
    source_words = _words(source_text)
    if not draft_words:
        return GuardVerdict(False, "draft is empty")
    if not source_words:
        return PASSED

    run = longest_verbatim_run(draft, source_text)
    if run > max_words:
        return GuardVerdict(
            False, f"verbatim run of {run} words exceeds the limit of {max_words}"
        )
    ratio = len(draft_words) / len(source_words)
    if ratio > max_ratio and len(source_words) >= 60:
        return GuardVerdict(
            False, f"draft length is {ratio:.2f} of the source, limit is {max_ratio}"
        )
    return PASSED


def _numbers(text: str) -> list[str]:
    collapsed = _DIGIT_GROUP_RE.sub("", normalize_text(text))
    return [
        number.lstrip("0") or number
        for number in _NUMBER_RE.findall(collapsed)
        if len(number) >= MIN_NUMBER_LENGTH
    ]


def unsupported_numbers(draft: str, source_text: str) -> list[str]:
    """Return multi-digit numbers present in the draft but absent from the source."""
    source_numbers = set(_numbers(source_text))
    missing: list[str] = []
    for number in _numbers(draft):
        if number not in source_numbers and number not in missing:
            missing.append(number)
    return missing


def check_no_invented_numbers(draft: str, source_text: str) -> GuardVerdict:
    """Reject a draft that introduces figures the source article does not contain."""
    if not normalize_text(source_text):
        return PASSED
    missing = unsupported_numbers(draft, source_text)
    if missing:
        return GuardVerdict(
            False, "numbers absent from the source: " + ", ".join(missing[:5])
        )
    return PASSED


def check_source_link(text: str, url: str) -> GuardVerdict:
    """Reject a publication body that does not contain the source link.

    The rendered post is Telegram HTML, so the url may legitimately appear in
    its escaped form; both spellings satisfy the mandatory link rule.
    """
    if not url:
        return GuardVerdict(False, "source url is missing")
    body = text or ""
    if url not in body and escape(url, quote=True) not in body:
        return GuardVerdict(False, "source link is missing from the publication")
    return PASSED


def check_draft(draft: str, source_text: str, *, title: str = "") -> GuardVerdict:
    """Run every draft guard and return the first failure.

    ``title`` is the Russian headline shown on the card and in the channel
    post; it is checked for invented figures the same way as the draft body,
    since a fabricated number in the headline is just as much an invented
    fact as one in the draft.
    """
    checks = [
        check_not_verbatim_copy(draft, source_text),
        check_no_invented_numbers(draft, source_text),
    ]
    if title:
        checks.append(check_no_invented_numbers(title, source_text))
    for verdict in checks:
        if not verdict.passed:
            return verdict
    return PASSED
