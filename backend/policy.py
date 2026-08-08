"""Rate limiting and content-governance policy for meme delivery."""

from __future__ import annotations

import math
import os
import time
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any


def _tokens(
    values: Any, maximum: int = 128, item_length: int = 128
) -> frozenset[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        token = " ".join(value.split()).strip().casefold()[:item_length]
        if token and token not in result:
            result.append(token)
        if len(result) >= maximum:
            break
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class PolicySettings:
    enabled: bool = True
    quota_window_seconds: float = 60.0
    quota_max_sends: int = 8
    blocked_tags: frozenset[str] = frozenset()
    allowed_tags: frozenset[str] = frozenset()
    blocked_namespaces: frozenset[str] = frozenset()
    blocked_ids: frozenset[str] = frozenset()
    max_file_bytes: int = 20 * 1024 * 1024

    @classmethod
    def safe(
        cls,
        enabled: Any = True,
        quota_window_seconds: Any = 60.0,
        quota_max_sends: Any = 8,
        blocked_tags: Any = (),
        allowed_tags: Any = (),
        blocked_namespaces: Any = (),
        blocked_ids: Any = (),
        max_file_bytes: Any = 20 * 1024 * 1024,
    ) -> "PolicySettings":
        if not isinstance(enabled, bool):
            enabled = True
        if isinstance(quota_window_seconds, bool) or not isinstance(
            quota_window_seconds, (int, float)
        ):
            quota_window_seconds = 60.0
        quota_window_seconds = float(quota_window_seconds)
        if not math.isfinite(quota_window_seconds):
            quota_window_seconds = 60.0
        quota_window_seconds = max(1.0, min(24 * 3600.0, quota_window_seconds))
        if isinstance(quota_max_sends, bool) or not isinstance(quota_max_sends, int):
            quota_max_sends = 8
        quota_max_sends = max(1, min(1000, quota_max_sends))
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int):
            max_file_bytes = 20 * 1024 * 1024
        max_file_bytes = max(1024, min(100 * 1024 * 1024, max_file_bytes))
        return cls(
            enabled=enabled,
            quota_window_seconds=quota_window_seconds,
            quota_max_sends=quota_max_sends,
            blocked_tags=_tokens(blocked_tags),
            allowed_tags=_tokens(allowed_tags),
            blocked_namespaces=_tokens(blocked_namespaces),
            blocked_ids=_tokens(blocked_ids, 256, 512),
            max_file_bytes=max_file_bytes,
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    candidates: tuple[dict[str, Any], ...] = ()
    reason: str = "ok"
    reservation_id: int | None = None


class MemePolicy:
    """Bounded per-scope quota and candidate filtering."""

    def __init__(
        self,
        settings: PolicySettings | None = None,
        *,
        max_scopes: int = 2048,
        clock=None,
    ) -> None:
        if max_scopes < 1:
            raise ValueError("max_scopes must be positive")
        self.settings = settings or PolicySettings()
        self.max_scopes = max_scopes
        self._clock = clock or time.time
        self._lock = RLock()
        self._reservations: OrderedDict[str, deque[tuple[int, float]]] = OrderedDict()
        self._next_reservation = 0
        self._denied = 0
        self._reserved = 0
        self._released = 0

    @staticmethod
    def _scope(scope: Any) -> str:
        if not isinstance(scope, str) or not scope.strip():
            return "global"
        return scope.strip()[:512]

    @staticmethod
    def _candidate_tags(candidate: dict[str, Any]) -> set[str]:
        raw = candidate.get("tags", [])
        if not isinstance(raw, (list, tuple, set, frozenset)):
            return set()
        return set(_tokens(raw, 256))

    @staticmethod
    def _candidate_namespace(candidate: dict[str, Any]) -> str:
        source = candidate.get("source")
        if isinstance(source, str) and source:
            return source.casefold()
        image_id = candidate.get("id")
        if isinstance(image_id, str):
            return image_id.split(":", 1)[0].casefold()
        return ""

    def _purge_locked(self, scope_key: str, now: float) -> deque[tuple[int, float]]:
        values = self._reservations.get(scope_key)
        if values is None:
            if len(self._reservations) >= self.max_scopes:
                self._reservations.popitem(last=False)
            values = deque()
            self._reservations[scope_key] = values
        while values and now - values[0][1] >= self.settings.quota_window_seconds:
            values.popleft()
        self._reservations.move_to_end(scope_key)
        return values

    def reserve(
        self,
        candidates: Iterable[dict[str, Any]],
        *,
        scope: Any = "global",
        query_tags: Iterable[Any] = (),
    ) -> PolicyDecision:
        values = [candidate for candidate in candidates if isinstance(candidate, dict)]
        if not values:
            return PolicyDecision(False, reason="no_candidates")
        if not self.settings.enabled:
            return PolicyDecision(True, tuple(values), "disabled")
        safe_query = _tokens(query_tags, 64)
        if self.settings.blocked_tags.intersection(safe_query):
            with self._lock:
                self._denied += 1
            return PolicyDecision(False, reason="blocked_query_tag")
        filtered: list[dict[str, Any]] = []
        for candidate in values:
            image_id = candidate.get("id")
            if not isinstance(image_id, str) or image_id.casefold() in self.settings.blocked_ids:
                continue
            tags = self._candidate_tags(candidate)
            if self.settings.blocked_tags.intersection(tags):
                continue
            if self.settings.allowed_tags and not self.settings.allowed_tags.intersection(tags):
                continue
            if self._candidate_namespace(candidate) in self.settings.blocked_namespaces:
                continue
            path_value = candidate.get("path")
            if not isinstance(path_value, str) or not path_value:
                continue
            try:
                path = Path(path_value).resolve(strict=True)
                stat_result = path.stat()
                if not os.path.isfile(path) or stat_result.st_size > self.settings.max_file_bytes:
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
            filtered.append(candidate)
        if not filtered:
            with self._lock:
                self._denied += 1
            return PolicyDecision(False, reason="content_blocked")
        now = max(0.0, float(self._clock()))
        with self._lock:
            reservations = self._purge_locked(self._scope(scope), now)
            if len(reservations) >= self.settings.quota_max_sends:
                self._denied += 1
                return PolicyDecision(False, reason="quota_exceeded")
            self._next_reservation += 1
            reservation_id = self._next_reservation
            reservations.append((reservation_id, now))
            self._reserved += 1
        return PolicyDecision(True, tuple(filtered), "ok", reservation_id)

    def release(self, scope: Any = "global", reservation_id: int | None = None) -> None:
        if not self.settings.enabled:
            return
        scope_key = self._scope(scope)
        with self._lock:
            reservations = self._reservations.get(scope_key)
            if not reservations:
                return
            if reservation_id is None:
                reservations.pop()
            else:
                for index in range(len(reservations) - 1, -1, -1):
                    if reservations[index][0] == reservation_id:
                        del reservations[index]
                        break
                else:
                    return
            self._released += 1
            if not reservations:
                self._reservations.pop(scope_key, None)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.settings.enabled,
                "quota_window_seconds": self.settings.quota_window_seconds,
                "quota_max_sends": self.settings.quota_max_sends,
                "blocked_tag_count": len(self.settings.blocked_tags),
                "allowed_tag_count": len(self.settings.allowed_tags),
                "blocked_namespace_count": len(self.settings.blocked_namespaces),
                "blocked_id_count": len(self.settings.blocked_ids),
                "max_file_bytes": self.settings.max_file_bytes,
                "tracked_scopes": len(self._reservations),
                "reserved": self._reserved,
                "released": self._released,
                "denied": self._denied,
            }

    def clear(self) -> None:
        with self._lock:
            self._reservations.clear()


__all__ = ["MemePolicy", "PolicyDecision", "PolicySettings"]
