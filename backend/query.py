"""Bounded query planning for low-effort, reliable meme tool calls."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable


_MAX_SOURCE_LENGTH = 512
_MAX_TERM_LENGTH = 128
_MAX_TERMS = 16
_MAX_KNOWN_TAGS = 4096
_TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,24}|[A-Za-z0-9][A-Za-z0-9_-]{1,31}")


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A bounded set of matcher terms plus their user-facing source text."""

    terms: tuple[str, ...] = ()
    source: str = ""
    used_context: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.terms


def build_query_plan(
    *,
    intent: Any = "",
    tags: Any = (),
    scene: Any = "",
    context: Any = "",
    known_tags: Iterable[Any] = (),
    max_terms: int = _MAX_TERMS,
) -> QueryPlan:
    """Normalise one-shot intent and legacy fields into bounded search terms.

    The planner does not attempt to be a language model.  It only performs
    deterministic, cheap operations that make a natural-language sentence
    useful to both keyword and embedding matchers:

    * use explicit ``intent``/legacy fields first;
    * if all are absent, use the active message as context;
    * promote known library tags found inside the sentence; and
    * retain a few lexical tokens as a final keyword fallback.
    """

    try:
        limit = max(1, min(_MAX_TERMS, int(max_terms)))
    except (TypeError, ValueError):
        limit = _MAX_TERMS
    intent_text = _clean_text(intent, 256)
    scene_text = _clean_text(scene, 200)
    tag_values = _clean_tags(tags)
    explicit = bool(intent_text or scene_text or tag_values)
    context_text = _clean_text(context, _MAX_SOURCE_LENGTH)
    used_context = not explicit and bool(context_text)
    if used_context:
        intent_text = context_text

    parts: list[str] = []
    if intent_text:
        parts.append(intent_text)
    parts.extend(tag_values)
    if scene_text:
        parts.append(scene_text)
    source = _join_source(parts)
    if not source:
        return QueryPlan(used_context=used_context)

    terms: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        cleaned = _clean_text(value, _MAX_TERM_LENGTH)
        key = cleaned.casefold()
        if cleaned and key not in seen and len(terms) < limit:
            seen.add(key)
            terms.append(cleaned)

    # Keep the full intent so embedding providers see the user's actual
    # request; explicit tags follow it for keyword evidence and auditability.
    if intent_text:
        add(intent_text)
    for value in tag_values:
        add(value)
    if scene_text:
        add(scene_text)

    source_key = source.casefold()
    known: list[str] = []
    try:
        known_values = list(known_tags)[:_MAX_KNOWN_TAGS]
    except (TypeError, ValueError):
        known_values = []
    for raw_tag in known_values:
        tag = _clean_text(raw_tag, _MAX_TERM_LENGTH)
        if tag and tag.casefold() in source_key:
            known.append(tag)
    # Prefer longer known tags so “无语吐槽” is not crowded out by “无语”.
    for tag in sorted(set(known), key=lambda value: (-len(value), value.casefold())):
        add(tag)

    for token in _TOKEN_PATTERN.findall(source):
        add(token)
    return QueryPlan(tuple(terms), source, used_context)


def _clean_text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("\x00", " ").split()).strip()
    return text[:maximum]


def _clean_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item, _MAX_TERM_LENGTH)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
        if len(result) >= 16:
            break
    return result


def _join_source(parts: Iterable[str]) -> str:
    return _clean_text(" ".join(part for part in parts if part), _MAX_SOURCE_LENGTH)


__all__ = ["QueryPlan", "build_query_plan"]
