"""Bounded, session-aware candidate selection for meme delivery."""

from __future__ import annotations

import hashlib
import math
import os
import random
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class SelectionSettings:
    mode: str = "weighted"
    pool_size: int = 5
    cooldown_seconds: float = 300.0
    history_size: int = 20
    deduplicate_files: bool = True
    max_hash_bytes: int = 100 * 1024 * 1024

    @classmethod
    def safe(
        cls,
        mode: Any = "weighted",
        pool_size: Any = 5,
        cooldown_seconds: Any = 300.0,
        history_size: Any = 20,
        deduplicate_files: Any = True,
    ) -> "SelectionSettings":
        """Normalise legacy configuration without allowing unsafe values."""

        if mode not in {"weighted", "top", "random"}:
            mode = "weighted"
        if isinstance(pool_size, bool) or not isinstance(pool_size, int):
            pool_size = 5
        pool_size = max(1, min(100, pool_size))
        if isinstance(cooldown_seconds, bool) or not isinstance(
            cooldown_seconds, (int, float)
        ):
            cooldown_seconds = 300.0
        cooldown_seconds = float(cooldown_seconds)
        if not math.isfinite(cooldown_seconds):
            cooldown_seconds = 300.0
        cooldown_seconds = max(0.0, min(30 * 24 * 3600.0, cooldown_seconds))
        if isinstance(history_size, bool) or not isinstance(history_size, int):
            history_size = 20
        history_size = max(1, min(1000, history_size))
        if not isinstance(deduplicate_files, bool):
            deduplicate_files = True
        return cls(
            mode=mode,
            pool_size=pool_size,
            cooldown_seconds=cooldown_seconds,
            history_size=history_size,
            deduplicate_files=deduplicate_files,
        )


@dataclass(frozen=True, slots=True)
class SelectionRecord:
    image_id: str
    content_key: str
    selected_at: float


class MemeSelector:
    """Select candidates with content de-duplication and bounded cooldown state."""

    def __init__(
        self,
        *,
        max_scopes: int = 1024,
        max_hash_entries: int = 4096,
        rng: random.Random | random.SystemRandom | None = None,
    ) -> None:
        if max_scopes < 1 or max_hash_entries < 1:
            raise ValueError("selector bounds must be positive")
        self.max_scopes = max_scopes
        self.max_hash_entries = max_hash_entries
        self.rng = rng or random.SystemRandom()
        self._recent: OrderedDict[str, deque[SelectionRecord]] = OrderedDict()
        self._hash_cache: OrderedDict[tuple[str, int, int, int], str] = OrderedDict()

    @property
    def scope_count(self) -> int:
        return len(self._recent)

    @property
    def hash_cache_count(self) -> int:
        return len(self._hash_cache)

    def choose(
        self,
        candidates: Iterable[dict[str, Any]],
        *,
        scope: str = "global",
        settings: SelectionSettings | None = None,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        settings = settings or SelectionSettings()
        current_time = time.time() if now is None else float(now)
        scope_key = self._normalise_scope(scope)
        unique: dict[str, tuple[dict[str, Any], float]] = {}
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            image_id = candidate.get("id")
            if not isinstance(image_id, str) or not image_id:
                continue
            content_key = (
                self._content_key(candidate)
                if settings.deduplicate_files
                else f"id:{image_id}"
            )
            score = self._score(
                candidate.get("selection_score", candidate.get("score"))
            )
            previous = unique.get(content_key)
            if previous is None or (score, image_id) > (previous[1], previous[0].get("id", "")):
                unique[content_key] = (candidate, score)
        if not unique:
            return None

        records = self._get_records(scope_key)
        self._purge_records(records, current_time, settings.cooldown_seconds)
        blocked_ids = {record.image_id for record in records}
        blocked_content = {record.content_key for record in records}
        available = [
            (key, candidate, score)
            for key, (candidate, score) in unique.items()
            if candidate.get("id") not in blocked_ids and key not in blocked_content
        ]
        if not available:
            # A single-item library must remain usable.  Repeating is allowed
            # only after every unique candidate is inside the cooldown window.
            available = [
                (key, candidate, score)
                for key, (candidate, score) in unique.items()
            ]

        available.sort(key=lambda row: (-row[2], str(row[1].get("id", ""))))
        pool = available[: settings.pool_size]
        if settings.mode == "top":
            selected_key, selected, _ = pool[0]
        elif settings.mode == "random":
            selected_key, selected, _ = self.rng.choice(pool)
        else:
            weights = self._weights(pool)
            selected_key, selected, _ = self.rng.choices(
                pool, weights=weights, k=1
            )[0]
        self._remember(
            scope_key,
            SelectionRecord(str(selected.get("id")), selected_key, current_time),
            settings.history_size,
        )
        return selected

    def release(self, candidate: dict[str, Any], *, scope: str = "global") -> None:
        """Undo the most recent reservation when sending a message fails."""

        scope_key = self._normalise_scope(scope)
        records = self._recent.get(scope_key)
        image_id = candidate.get("id") if isinstance(candidate, dict) else None
        if records is None or not isinstance(image_id, str):
            return
        # A send failure belongs to the reservation made by the current call.
        # Remove only the newest matching reservation so an older successful
        # send remains part of the cooldown history.
        kept = deque(records)
        for index in range(len(kept) - 1, -1, -1):
            if kept[index].image_id == image_id:
                del kept[index]
                break
        if kept:
            self._recent[scope_key] = kept
        else:
            self._recent.pop(scope_key, None)

    def clear(self, scope: str | None = None) -> None:
        if scope is None:
            self._recent.clear()
            self._hash_cache.clear()
            return
        self._recent.pop(self._normalise_scope(scope), None)

    def status(self) -> dict[str, int]:
        return {
            "scope_count": len(self._recent),
            "recent_count": sum(len(records) for records in self._recent.values()),
            "hash_cache_count": len(self._hash_cache),
        }

    def _get_records(self, scope: str) -> deque[SelectionRecord]:
        records = self._recent.get(scope)
        if records is None:
            records = deque()
            self._recent[scope] = records
        self._recent.move_to_end(scope)
        while len(self._recent) > self.max_scopes:
            self._recent.popitem(last=False)
        return records

    @staticmethod
    def _purge_records(
        records: deque[SelectionRecord], now: float, cooldown_seconds: float
    ) -> None:
        while records and now - records[0].selected_at >= cooldown_seconds:
            records.popleft()

    def _remember(self, scope: str, record: SelectionRecord, history_size: int) -> None:
        records = self._get_records(scope)
        records.append(record)
        while len(records) > history_size:
            records.popleft()

    @staticmethod
    def _weights(pool: list[tuple[str, dict[str, Any], float]]) -> list[float]:
        scores = [score for _, _, score in pool]
        floor = min(scores)
        return [max(score - floor, 0.0) + 0.01 for score in scores]

    def _content_key(self, candidate: dict[str, Any]) -> str:
        image_id = candidate.get("id")
        path_value = candidate.get("path")
        if not isinstance(image_id, str):
            return "invalid"
        if not isinstance(path_value, str) or not path_value:
            return f"id:{image_id}"
        try:
            path = Path(path_value).resolve(strict=True)
            stat_result = path.stat()
            if not os.path.isfile(path) or stat_result.st_size > SelectionSettings().max_hash_bytes:
                return f"id:{image_id}"
            key = (
                os.path.normcase(os.fspath(path)),
                int(stat_result.st_size),
                int(stat_result.st_mtime_ns),
                int(getattr(stat_result, "st_ino", 0)),
            )
            cached = self._hash_cache.get(key)
            if cached is not None:
                self._hash_cache.move_to_end(key)
                return cached
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            content_key = f"sha256:{digest.hexdigest()}"
            self._hash_cache[key] = content_key
            self._hash_cache.move_to_end(key)
            while len(self._hash_cache) > self.max_hash_entries:
                self._hash_cache.popitem(last=False)
            return content_key
        except (OSError, RuntimeError, ValueError):
            return f"id:{image_id}"

    @staticmethod
    def _score(value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return 0.0
        return score if math.isfinite(score) else 0.0

    @staticmethod
    def _normalise_scope(scope: Any) -> str:
        if not isinstance(scope, str) or not scope.strip():
            return "global"
        return scope.strip()[:512]


__all__ = ["MemeSelector", "SelectionRecord", "SelectionSettings"]
