"""Pack, persona and sticky-session routing for meme candidates."""

from __future__ import annotations

import hashlib
import math
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


_PACK_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_MAX_PACKS = 32
_MAX_PERSONAS = 64
_MAX_FILTER_VALUES = 32


class RoutingConfigError(ValueError):
    """Raised when a pack configuration cannot be used safely."""


def _token(value: Any, maximum: int = 64) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip().casefold()[:maximum]


def _values(value: Any, maximum: int = _MAX_FILTER_VALUES) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    result: list[str] = []
    for item in value:
        parsed = _token(item, 128)
        if parsed and parsed not in result:
            result.append(parsed)
        if len(result) >= maximum:
            break
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class PackDefinition:
    id: str
    label: str
    namespaces: frozenset[str] = frozenset()
    include_tags: frozenset[str] = frozenset()
    exclude_tags: frozenset[str] = frozenset()
    personas: frozenset[str] = frozenset()
    weight: float = 1.0
    enabled: bool = True

    @classmethod
    def from_value(cls, value: Mapping[str, Any], number: int = 0) -> "PackDefinition":
        if not isinstance(value, Mapping):
            raise RoutingConfigError(f"meme_packs[{number}] 必须是对象")
        pack_id = _token(value.get("id"), 32)
        if not _PACK_ID.fullmatch(pack_id):
            raise RoutingConfigError(f"meme_packs[{number}].id 格式无效")
        label = _token(value.get("label", pack_id), 96) or pack_id
        raw_weight = value.get("weight", 1.0)
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, (int, float)):
            raise RoutingConfigError(f"meme_packs[{number}].weight 必须是数字")
        weight = float(raw_weight)
        if not math.isfinite(weight) or not 0.0 <= weight <= 100.0:
            raise RoutingConfigError(f"meme_packs[{number}].weight 超出范围")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RoutingConfigError(f"meme_packs[{number}].enabled 必须是布尔值")
        personas = _values(value.get("personas", []), 16)
        allowed = {
            "id",
            "label",
            "namespaces",
            "include_tags",
            "exclude_tags",
            "personas",
            "weight",
            "enabled",
        }
        unknown = set(value) - allowed
        if unknown:
            raise RoutingConfigError(f"meme_packs[{number}] 包含不支持的字段")
        return cls(
            id=pack_id,
            label=label,
            namespaces=_values(value.get("namespaces", [])),
            include_tags=_values(value.get("include_tags", [])),
            exclude_tags=_values(value.get("exclude_tags", [])),
            personas=personas,
            weight=weight,
            enabled=enabled,
        )


@dataclass(frozen=True, slots=True)
class RoutingSettings:
    packs: tuple[PackDefinition, ...] = ()
    default_pack: str = ""
    persona_packs: tuple[tuple[str, str], ...] = ()
    sticky_sessions: bool = True

    @classmethod
    def safe(
        cls,
        packs: Any = (),
        default_pack: Any = "",
        persona_packs: Any = None,
        sticky_sessions: Any = True,
    ) -> "RoutingSettings":
        parsed: list[PackDefinition] = []
        seen: set[str] = set()
        if isinstance(packs, (list, tuple)):
            for number, value in enumerate(packs[:_MAX_PACKS]):
                try:
                    pack = PackDefinition.from_value(value, number)
                except RoutingConfigError:
                    continue
                if pack.id in seen:
                    continue
                seen.add(pack.id)
                parsed.append(pack)
        normalized_default = _token(default_pack, 32)
        if normalized_default not in seen:
            normalized_default = ""
        aliases: list[tuple[str, str]] = []
        for pack in parsed:
            aliases.extend((persona, pack.id) for persona in pack.personas)
        if isinstance(persona_packs, Mapping):
            for raw_persona, raw_pack in list(persona_packs.items())[:_MAX_PERSONAS]:
                persona = _token(raw_persona, 64)
                pack_id = _token(raw_pack, 32)
                if persona and pack_id in seen:
                    aliases = [item for item in aliases if item[0] != persona]
                    aliases.append((persona, pack_id))
        return cls(
            packs=tuple(parsed),
            default_pack=normalized_default,
            persona_packs=tuple(aliases),
            sticky_sessions=sticky_sessions if isinstance(sticky_sessions, bool) else True,
        )

    def to_config(self) -> dict[str, Any]:
        return {
            "meme_packs": [
                {
                    "id": pack.id,
                    "label": pack.label,
                    "namespaces": sorted(pack.namespaces),
                    "include_tags": sorted(pack.include_tags),
                    "exclude_tags": sorted(pack.exclude_tags),
                    "personas": sorted(pack.personas),
                    "weight": pack.weight,
                    "enabled": pack.enabled,
                }
                for pack in self.packs
            ],
            "default_pack": self.default_pack,
            "persona_packs": dict(self.persona_packs),
            "sticky_sessions": self.sticky_sessions,
        }


class MemeRouter:
    """Route candidates without ever making an empty pack fatal."""

    def __init__(self, settings: RoutingSettings | None = None, *, max_scopes: int = 2048):
        if max_scopes < 1:
            raise ValueError("max_scopes must be positive")
        self.settings = settings or RoutingSettings()
        self.max_scopes = max_scopes
        self._assignments: OrderedDict[str, str] = OrderedDict()
        self._route_counts: dict[str, int] = {}

    def update(self, settings: RoutingSettings) -> None:
        self.settings = settings
        self.clear()

    @staticmethod
    def _scope(scope: Any) -> str:
        if not isinstance(scope, str) or not scope.strip():
            return "global"
        return scope.strip()[:512]

    def _enabled(self) -> list[PackDefinition]:
        return [pack for pack in self.settings.packs if pack.enabled and pack.weight > 0]

    def _find(self, pack_id: str) -> PackDefinition | None:
        return next((pack for pack in self._enabled() if pack.id == pack_id), None)

    def _sticky_pack(self, scope: str) -> PackDefinition | None:
        packs = self._enabled()
        if not packs:
            return None
        existing = self._assignments.get(scope)
        selected = self._find(existing) if existing else None
        if selected is None:
            digest = hashlib.sha256(scope.encode("utf-8", "replace")).digest()
            bucket = int.from_bytes(digest[:8], "big") / float(2**64)
            total = sum(pack.weight for pack in packs)
            cursor = bucket * total
            selected = packs[-1]
            for pack in packs:
                cursor -= pack.weight
                if cursor <= 0:
                    selected = pack
                    break
            self._assignments[scope] = selected.id
            self._assignments.move_to_end(scope)
            while len(self._assignments) > self.max_scopes:
                self._assignments.popitem(last=False)
        return selected

    @staticmethod
    def _matches(pack: PackDefinition, candidate: Mapping[str, Any]) -> bool:
        source = candidate.get("source")
        if not isinstance(source, str) or not source:
            image_id = candidate.get("id", "")
            source = image_id.split(":", 1)[0] if isinstance(image_id, str) else ""
        if pack.namespaces and source.casefold() not in pack.namespaces:
            return False
        raw_tags = candidate.get("tags", [])
        tags = (
            {_token(tag, 128) for tag in raw_tags}
            if isinstance(raw_tags, (list, tuple, set, frozenset))
            else set()
        )
        if pack.include_tags and not pack.include_tags.issubset(tags):
            return False
        if pack.exclude_tags.intersection(tags):
            return False
        return True

    def route(
        self,
        candidates: Iterable[dict[str, Any]],
        *,
        scope: Any = "global",
        pack: Any = "",
        persona: Any = "",
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        values = [candidate for candidate in candidates if isinstance(candidate, dict)]
        scope_key = self._scope(scope)
        requested_pack = _token(pack, 32)
        requested_persona = _token(persona, 64)
        if not requested_pack and requested_persona:
            requested_pack = dict(self.settings.persona_packs).get(requested_persona, "")
        selected = self._find(requested_pack) if requested_pack else None
        if selected is None and self.settings.default_pack:
            selected = self._find(self.settings.default_pack)
        if selected is None and self.settings.sticky_sessions:
            selected = self._sticky_pack(scope_key)
        if selected is None:
            return values, {"pack": "", "fallback": False, "reason": "no_packs"}
        filtered = [candidate for candidate in values if self._matches(selected, candidate)]
        fallback = not filtered and bool(values)
        result = filtered or values
        self._route_counts[selected.id] = self._route_counts.get(selected.id, 0) + 1
        return result, {
            "pack": selected.id,
            "label": selected.label,
            "fallback": fallback,
            "reason": "empty_pack" if fallback else "matched",
        }

    def status(self) -> dict[str, Any]:
        return {
            "packs": [
                {
                    "id": pack.id,
                    "label": pack.label,
                    "enabled": pack.enabled,
                    "namespaces": sorted(pack.namespaces),
                    "include_tags": sorted(pack.include_tags),
                    "exclude_tags": sorted(pack.exclude_tags),
                    "personas": sorted(pack.personas),
                    "weight": pack.weight,
                }
                for pack in self.settings.packs
            ],
            "default_pack": self.settings.default_pack,
            "persona_packs": dict(self.settings.persona_packs),
            "sticky_sessions": self.settings.sticky_sessions,
            "assigned_sessions": len(self._assignments),
            "route_counts": dict(self._route_counts),
        }

    def clear(self) -> None:
        self._assignments.clear()
        self._route_counts.clear()


__all__ = [
    "MemeRouter",
    "PackDefinition",
    "RoutingConfigError",
    "RoutingSettings",
]
