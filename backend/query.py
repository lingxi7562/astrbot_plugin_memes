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
_CLAUSE_PATTERN = re.compile(
    r"([，,；;。!?！？]+|但是|然而|不过|可是|但|却|反而|只是)"
)
_CONTRAST_MARKERS = frozenset({"但是", "然而", "不过", "可是", "但", "却", "反而", "只是"})
_SELF_PATTERN = re.compile(r"(?:我|我们|本人|咱们|自己)")
_REPORTED_PATTERN = re.compile(r"(?:用户|对方|他人|别人|他说|她说|他们|他|她)")
_STRONG_PATTERN = re.compile(r"(?:很|非常|特别|超级|太|极其)")
_HEDGE_PATTERN = re.compile(r"(?:有些|有点|稍微|略|似乎)")


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A bounded set of matcher terms plus their user-facing source text."""

    terms: tuple[str, ...] = ()
    source: str = ""
    used_context: bool = False
    focus: str = ""
    primary_terms: tuple[str, ...] = ()
    focus_reason: str = ""

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

    try:
        known_values = list(known_tags)[:_MAX_KNOWN_TAGS]
    except (TypeError, ValueError):
        known_values = []
    focus, primary_terms, focus_reason = _choose_focus(source, known_values)

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
    # Prefer the focus clause, then longer known tags so the main emotion is
    # not crowded out by a reported/secondary emotion.
    primary_keys = {tag.casefold() for tag in primary_terms}
    for tag in list(primary_terms) + sorted(
        (value for value in set(known) if value.casefold() not in primary_keys),
        key=lambda value: (-len(value), value.casefold()),
    ):
        add(tag)

    for token in _TOKEN_PATTERN.findall(source):
        add(token)
    return QueryPlan(
        tuple(terms),
        source,
        used_context,
        focus,
        tuple(primary_terms),
        focus_reason,
    )


def _choose_focus(
    source: str, known_values: list[Any]
) -> tuple[str, tuple[str, ...], str]:
    """Pick the likely speaker-focused clause without pretending certainty.

    Contrast tails (``但/但是/不过/然而``) and first-person language are
    strong signals for the assistant's intended reaction.  Reported subjects
    such as ``用户/对方`` are down-weighted, especially when softened by
    ``有些/有点``.  If no known tag occurs, the selected clause is still
    returned for diagnostics while ``primary_terms`` stays empty.
    """

    segments = _split_clauses(source)
    known_tags = []
    seen: set[str] = set()
    for raw in known_values:
        tag = _clean_text(raw, _MAX_TERM_LENGTH)
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            known_tags.append(tag)
    candidates: list[tuple[float, int, str, tuple[str, ...], str]] = []
    for index, (segment, after_contrast) in enumerate(segments):
        lowered = segment.casefold()
        matched = [tag for tag in known_tags if tag.casefold() in lowered]
        speaker = bool(_SELF_PATTERN.search(segment))
        reported = bool(_REPORTED_PATTERN.search(segment))
        strong = bool(_STRONG_PATTERN.search(segment))
        hedge = bool(_HEDGE_PATTERN.search(segment))
        base = (3.0 if after_contrast else 0.0) + (2.0 if speaker else 0.0)
        base += 0.6 if strong else 0.0
        base -= 1.25 if reported else 0.0
        base -= 0.45 if hedge else 0.0
        # Later clauses are a small tie-breaker, never a replacement for
        # speaker/contrast evidence.
        base += min(index, 8) * 0.01
        if matched:
            scored = sorted(
                matched,
                key=lambda tag: (
                    -_emotion_strength(segment, tag),
                    -len(tag),
                    tag.casefold(),
                ),
            )
            base += max(_emotion_strength(segment, tag) for tag in matched)
            reason = "contrast_tail" if after_contrast else ("speaker" if speaker else "tag")
            candidates.append((base, index, segment.strip(), tuple(scored), reason))
        elif speaker or after_contrast:
            reason = "contrast_tail" if after_contrast else "speaker"
            candidates.append((base, index, segment.strip(), (), reason))
    if not candidates:
        fallback = segments[-1][0].strip() if segments else source
        return fallback, (), "fallback"
    chosen = max(candidates, key=lambda row: (row[0], row[1]))
    return chosen[2], chosen[3], chosen[4]


def _split_clauses(source: str) -> list[tuple[str, bool]]:
    pieces = _CLAUSE_PATTERN.split(source)
    segments: list[tuple[str, bool]] = []
    buffer = ""
    after_contrast = False
    for piece in pieces:
        if not piece:
            continue
        if piece in _CONTRAST_MARKERS:
            if buffer.strip():
                segments.append((buffer.strip(), after_contrast))
                buffer = ""
            after_contrast = True
        elif re.fullmatch(r"[，,；;。!?！？]+", piece):
            if buffer.strip():
                segments.append((buffer.strip(), after_contrast))
                buffer = ""
            after_contrast = False
        else:
            buffer += piece
    if buffer.strip():
        segments.append((buffer.strip(), after_contrast))
    return segments or [(source.strip(), False)]


def _emotion_strength(segment: str, tag: str) -> float:
    position = segment.casefold().find(tag.casefold())
    if position < 0:
        return 0.0
    prefix = segment[max(0, position - 8) : position]
    strength = 0.6 if _STRONG_PATTERN.search(prefix) else 0.0
    strength -= 0.45 if _HEDGE_PATTERN.search(prefix) else 0.0
    return strength


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
