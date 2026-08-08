"""Bounded delivery analytics, feedback and per-session personalization."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any


@dataclass(frozen=True, slots=True)
class AnalyticsSettings:
    enabled: bool = True
    retention_days: int = 30
    personalization_strength: float = 0.5
    max_images: int = 10_000
    max_scopes: int = 1_024
    max_events: int = 5_000

    @classmethod
    def safe(
        cls,
        enabled: Any = True,
        retention_days: Any = 30,
        personalization_strength: Any = 0.5,
    ) -> "AnalyticsSettings":
        if not isinstance(enabled, bool):
            enabled = True
        if isinstance(retention_days, bool) or not isinstance(retention_days, int):
            retention_days = 30
        retention_days = max(1, min(365, retention_days))
        if isinstance(personalization_strength, bool) or not isinstance(
            personalization_strength, (int, float)
        ):
            personalization_strength = 0.5
        personalization_strength = float(personalization_strength)
        if not math.isfinite(personalization_strength):
            personalization_strength = 0.5
        personalization_strength = max(0.0, min(2.0, personalization_strength))
        return cls(
            enabled=enabled,
            retention_days=retention_days,
            personalization_strength=personalization_strength,
        )


def _empty_state() -> dict[str, Any]:
    return {
        "version": 1,
        "totals": {
            "sends": 0,
            "failures": 0,
            "feedback": 0,
            "last_activity": 0.0,
        },
        "images": {},
        "scopes": {},
        "events": [],
    }


class MemeAnalytics:
    """Persist small, privacy-conscious aggregates instead of raw messages.

    Scope identifiers are hashed before storage.  All maps and the event tail
    have explicit limits, and updates use a same-directory atomic replacement.
    A corrupt or oversized state file is ignored so analytics can never prevent
    the plugin from starting or sending a meme.
    """

    MAX_STATE_BYTES = 16 * 1024 * 1024
    MAX_ID_LENGTH = 512
    MAX_TAG_LENGTH = 128
    MAX_TAGS = 32

    def __init__(
        self,
        path: str | Path,
        settings: AnalyticsSettings | None = None,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path)
        self.settings = settings or AnalyticsSettings()
        self._clock = clock or time.time
        self._lock = RLock()
        self._state: dict[str, Any] = _empty_state()
        self._persist_errors = 0
        self._load()

    @staticmethod
    def _safe_id(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return value.strip()[:MemeAnalytics.MAX_ID_LENGTH]

    @staticmethod
    def _safe_tags(values: Iterable[Any]) -> list[str]:
        if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
            return []
        tags: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            tag = " ".join(value.split()).strip()[: MemeAnalytics.MAX_TAG_LENGTH]
            key = tag.casefold()
            if tag and key not in seen:
                seen.add(key)
                tags.append(key)
            if len(tags) >= MemeAnalytics.MAX_TAGS:
                break
        return tags

    @staticmethod
    def _scope_key(scope: Any) -> str:
        raw = scope.strip() if isinstance(scope, str) else "global"
        raw = raw[:512] or "global"
        digest = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
        return f"s:{digest[:32]}"

    @staticmethod
    def _bounded_int(value: Any, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return minimum
        return max(minimum, min(maximum, value))

    @staticmethod
    def _bounded_float(value: Any) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return 0.0
        return parsed if math.isfinite(parsed) and parsed >= 0 else 0.0

    def _load(self) -> None:
        try:
            if not self.path.is_file() or self.path.stat().st_size > self.MAX_STATE_BYTES:
                return
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            self._state = self._sanitize_state(loaded)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._state = _empty_state()

    def _sanitize_state(self, loaded: Any) -> dict[str, Any]:
        if not isinstance(loaded, Mapping) or loaded.get("version") != 1:
            return _empty_state()
        clean = _empty_state()
        raw_totals = loaded.get("totals")
        if isinstance(raw_totals, Mapping):
            clean["totals"] = {
                "sends": self._bounded_int(raw_totals.get("sends")),
                "failures": self._bounded_int(raw_totals.get("failures")),
                "feedback": self._bounded_int(raw_totals.get("feedback")),
                "last_activity": self._bounded_float(raw_totals.get("last_activity")),
            }
        raw_images = loaded.get("images")
        if isinstance(raw_images, Mapping):
            for raw_id, raw_record in list(raw_images.items())[: self.settings.max_images]:
                image_id = self._safe_id(raw_id)
                if image_id and isinstance(raw_record, Mapping):
                    clean["images"][image_id] = self._sanitize_image_record(raw_record)
        raw_scopes = loaded.get("scopes")
        if isinstance(raw_scopes, Mapping):
            for raw_scope, raw_record in list(raw_scopes.items())[: self.settings.max_scopes]:
                if isinstance(raw_scope, str) and raw_scope.startswith("s:"):
                    clean["scopes"][raw_scope] = self._sanitize_scope_record(raw_record)
        raw_events = loaded.get("events")
        if isinstance(raw_events, list):
            for event in raw_events[-self.settings.max_events :]:
                if not isinstance(event, Mapping):
                    continue
                kind = event.get("kind")
                image_id = self._safe_id(event.get("image_id"))
                at = self._bounded_float(event.get("at"))
                if kind in {"send", "failure", "feedback"} and image_id and at:
                    item = {"kind": kind, "image_id": image_id, "at": at}
                    if kind == "feedback" and event.get("rating") in {-1, 1}:
                        item["rating"] = event["rating"]
                    clean["events"].append(item)
        return clean

    def _sanitize_image_record(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "s": self._bounded_int(raw.get("s")),
            "f": self._bounded_int(raw.get("f")),
            "p": self._bounded_int(raw.get("p")),
            "n": self._bounded_int(raw.get("n")),
            "last": self._bounded_float(raw.get("last")),
        }

    def _sanitize_scope_record(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            return {"s": 0, "f": 0, "last": 0.0, "images": {}, "tags": {}}
        record: dict[str, Any] = {
            "s": self._bounded_int(raw.get("s")),
            "f": self._bounded_int(raw.get("f")),
            "last": self._bounded_float(raw.get("last")),
            "images": {},
            "tags": {},
        }
        raw_scope_images = raw.get("images")
        if isinstance(raw_scope_images, Mapping):
            for raw_id, raw_image in list(raw_scope_images.items())[:256]:
                image_id = self._safe_id(raw_id)
                if image_id and isinstance(raw_image, Mapping):
                    record["images"][image_id] = {
                        "s": self._bounded_int(raw_image.get("s")),
                        "p": self._bounded_int(raw_image.get("p")),
                        "n": self._bounded_int(raw_image.get("n")),
                        "last": self._bounded_float(raw_image.get("last")),
                    }
        raw_tags = raw.get("tags")
        if isinstance(raw_tags, Mapping):
            for raw_tag, raw_bias in list(raw_tags.items())[:256]:
                if isinstance(raw_tag, str) and isinstance(raw_bias, Mapping):
                    tag = raw_tag[: self.MAX_TAG_LENGTH]
                    record["tags"][tag] = {
                        "b": max(-10.0, min(10.0, float(raw_bias.get("b", 0.0)))),
                        "last": self._bounded_float(raw_bias.get("last")),
                    }
        return record

    def _scope_locked(self, scope: Any) -> dict[str, Any]:
        key = self._scope_key(scope)
        scopes = self._state["scopes"]
        record = scopes.get(key)
        if not isinstance(record, dict):
            if len(scopes) >= self.settings.max_scopes:
                oldest = min(
                    scopes.items(), key=lambda pair: self._bounded_float(pair[1].get("last"))
                )[0]
                scopes.pop(oldest, None)
            record = {"s": 0, "f": 0, "last": 0.0, "images": {}, "tags": {}}
            scopes[key] = record
        return record

    @staticmethod
    def _scope_image_locked(session: dict[str, Any], image_id: str) -> dict[str, Any]:
        images = session["images"]
        record = images.get(image_id)
        if isinstance(record, dict):
            return record
        if len(images) >= 256:
            oldest = min(images.items(), key=lambda pair: float(pair[1].get("last", 0.0)))[0]
            images.pop(oldest, None)
        record = {"s": 0, "p": 0, "n": 0, "last": 0.0}
        images[image_id] = record
        return record

    @staticmethod
    def _scope_tag_locked(session: dict[str, Any], tag: str) -> dict[str, Any]:
        tags = session["tags"]
        record = tags.get(tag)
        if isinstance(record, dict):
            return record
        if len(tags) >= 256:
            oldest = min(tags.items(), key=lambda pair: float(pair[1].get("last", 0.0)))[0]
            tags.pop(oldest, None)
        record = {"b": 0.0, "last": 0.0}
        tags[tag] = record
        return record

    def _image_locked(self, image_id: str) -> dict[str, Any] | None:
        images = self._state["images"]
        record = images.get(image_id)
        if isinstance(record, dict):
            return record
        if len(images) >= self.settings.max_images:
            oldest = min(
                images.items(), key=lambda pair: self._bounded_float(pair[1].get("last"))
            )[0]
            images.pop(oldest, None)
        record = {"s": 0, "f": 0, "p": 0, "n": 0, "last": 0.0}
        images[image_id] = record
        return record

    def _event_locked(self, kind: str, image_id: str, now: float, rating: int | None = None) -> None:
        event: dict[str, Any] = {"kind": kind, "image_id": image_id, "at": now}
        if rating in {-1, 1}:
            event["rating"] = rating
        events = self._state["events"]
        events.append(event)
        del events[: max(0, len(events) - self.settings.max_events)]

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self.settings.retention_days * 86400
        events = self._state["events"]
        self._state["events"] = [event for event in events if event.get("at", 0) >= cutoff]
        scopes = self._state["scopes"]
        for scope_key, scope in list(scopes.items()):
            if self._bounded_float(scope.get("last")) < cutoff:
                scopes.pop(scope_key, None)
                continue
            for image_id, image in list(scope.get("images", {}).items()):
                if self._bounded_float(image.get("last")) < cutoff:
                    scope["images"].pop(image_id, None)
            for tag, bias in list(scope.get("tags", {}).items()):
                if self._bounded_float(bias.get("last")) < cutoff:
                    scope["tags"].pop(tag, None)

    def _persist_locked(self) -> bool:
        temporary: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(self._state, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            temporary = None
            return True
        except (OSError, TypeError, ValueError):
            self._persist_errors += 1
            return False
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _touch_totals_locked(self, field: str, now: float) -> None:
        totals = self._state["totals"]
        totals[field] = self._bounded_int(totals.get(field)) + 1
        totals["last_activity"] = now

    def record_send(self, scope: Any, image_id: Any, tags: Iterable[Any] = ()) -> None:
        if not self.settings.enabled:
            return
        safe_id = self._safe_id(image_id)
        if not safe_id:
            return
        now = max(0.0, float(self._clock()))
        with self._lock:
            self._prune_locked(now)
            image = self._image_locked(safe_id)
            image["s"] += 1
            image["last"] = now
            session = self._scope_locked(scope)
            session["s"] += 1
            session["last"] = now
            session_image = self._scope_image_locked(session, safe_id)
            session_image["s"] += 1
            session_image["last"] = now
            self._touch_totals_locked("sends", now)
            self._event_locked("send", safe_id, now)
            self._persist_locked()

    def record_failure(self, scope: Any, image_id: Any) -> None:
        if not self.settings.enabled:
            return
        safe_id = self._safe_id(image_id)
        if not safe_id:
            return
        now = max(0.0, float(self._clock()))
        with self._lock:
            self._prune_locked(now)
            image = self._image_locked(safe_id)
            image["f"] += 1
            image["last"] = now
            session = self._scope_locked(scope)
            session["f"] += 1
            session["last"] = now
            self._touch_totals_locked("failures", now)
            self._event_locked("failure", safe_id, now)
            self._persist_locked()

    def record_feedback(
        self,
        scope: Any,
        image_id: Any,
        rating: Any,
        tags: Iterable[Any] = (),
    ) -> bool:
        if not self.settings.enabled:
            return False
        safe_id = self._safe_id(image_id)
        if (
            not safe_id
            or isinstance(rating, bool)
            or not isinstance(rating, int)
            or rating not in {-1, 1}
        ):
            return False
        now = max(0.0, float(self._clock()))
        safe_rating = int(rating)
        safe_tags = self._safe_tags(tags)
        with self._lock:
            self._prune_locked(now)
            image = self._image_locked(safe_id)
            image["p" if safe_rating > 0 else "n"] += 1
            image["last"] = now
            session = self._scope_locked(scope)
            session["last"] = now
            session_image = self._scope_image_locked(session, safe_id)
            session_image["p" if safe_rating > 0 else "n"] += 1
            session_image["last"] = now
            for tag in safe_tags:
                bias = self._scope_tag_locked(session, tag)
                bias["b"] = max(-10.0, min(10.0, float(bias["b"]) + safe_rating))
                bias["last"] = now
            self._touch_totals_locked("feedback", now)
            self._event_locked("feedback", safe_id, now, safe_rating)
            self._persist_locked()
        return True

    def personalize(
        self,
        candidates: Iterable[dict[str, Any]],
        *,
        scope: Any = "global",
    ) -> list[dict[str, Any]]:
        values = [candidate for candidate in candidates if isinstance(candidate, dict)]
        if not self.settings.enabled or self.settings.personalization_strength <= 0:
            return list(values)
        key = self._scope_key(scope)
        with self._lock:
            session = self._state["scopes"].get(key, {})
            global_session = self._state["scopes"].get(self._scope_key("global"), {})
            sessions = [session]
            if global_session is not session:
                sessions.append(global_session)
            strength = self.settings.personalization_strength
            result: list[dict[str, Any]] = []
            for candidate in values:
                copy = dict(candidate)
                image_id = self._safe_id(candidate.get("id"))
                positive = sum(
                    self._bounded_int(session_item.get("images", {}).get(image_id, {}).get("p"))
                    for session_item in sessions
                )
                negative = sum(
                    self._bounded_int(session_item.get("images", {}).get(image_id, {}).get("n"))
                    for session_item in sessions
                )
                image_bias = (positive - negative) / (positive + negative + 2.0)
                raw_tags = candidate.get("tags", [])
                tags = self._safe_tags(raw_tags if isinstance(raw_tags, list) else [])
                tag_biases = [
                    sum(
                        float(session_item.get("tags", {}).get(tag, {}).get("b", 0.0))
                        for session_item in sessions
                    )
                    for tag in tags
                ]
                tag_bias = sum(tag_biases) / (len(tag_biases) or 1) / 10.0
                preference = max(-1.0, min(1.0, 0.7 * image_bias + 0.3 * tag_bias))
                base = self._score(candidate.get("score"))
                copy["selection_score"] = base + strength * preference
                result.append(copy)
            return result

    def report(self, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(100, int(limit) if isinstance(limit, int) else 20))
        with self._lock:
            images = []
            for image_id, record in self._state["images"].items():
                images.append(
                    {
                        "id": image_id,
                        "sends": self._bounded_int(record.get("s")),
                        "failures": self._bounded_int(record.get("f")),
                        "positive": self._bounded_int(record.get("p")),
                        "negative": self._bounded_int(record.get("n")),
                        "last_activity": self._bounded_float(record.get("last")),
                    }
                )
            images.sort(key=lambda item: (-item["sends"], -item["positive"], item["id"]))
            totals = deepcopy(self._state["totals"])
            return {
                "enabled": self.settings.enabled,
                "retention_days": self.settings.retention_days,
                "personalization_strength": self.settings.personalization_strength,
                "totals": totals,
                "tracked_images": len(self._state["images"]),
                "tracked_scopes": len(self._state["scopes"]),
                "event_count": len(self._state["events"]),
                "persistence_errors": self._persist_errors,
                "top_images": images[:limit],
            }

    def reset(self) -> bool:
        with self._lock:
            self._state = _empty_state()
            return self._persist_locked()

    @staticmethod
    def _score(value: Any) -> float:
        try:
            score = float(value)
        except (TypeError, ValueError):
            return 0.0
        return score if math.isfinite(score) else 0.0


__all__ = ["AnalyticsSettings", "MemeAnalytics"]
