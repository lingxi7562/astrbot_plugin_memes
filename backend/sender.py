"""Compatibility-aware, bounded image sending pipeline."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True, slots=True)
class SendPipelineSettings:
    mode: str = "auto"
    timeout_seconds: float = 30.0
    retry_count: int = 0

    @classmethod
    def safe(
        cls,
        mode: Any = "auto",
        timeout_seconds: Any = 30.0,
        retry_count: Any = 0,
    ) -> "SendPipelineSettings":
        if mode not in {"auto", "chain", "image_result"}:
            mode = "auto"
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float)
        ):
            timeout_seconds = 30.0
        timeout_seconds = float(timeout_seconds)
        if not 1.0 <= timeout_seconds <= 120.0:
            timeout_seconds = 30.0
        if isinstance(retry_count, bool) or not isinstance(retry_count, int):
            retry_count = 0
        retry_count = max(0, min(2, retry_count))
        return cls(
            mode=mode,
            timeout_seconds=timeout_seconds,
            retry_count=retry_count,
        )


class MemeSender:
    """Use the newest event API when available and keep fallback explicit."""

    def __init__(
        self,
        settings: SendPipelineSettings | None = None,
        image_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.settings = settings or SendPipelineSettings()
        self._image_factory = image_factory

    async def send(self, event: Any, path: str | Path) -> None:
        image_path = Path(path)
        if not image_path.is_file():
            raise FileNotFoundError(str(image_path))
        last_error: Exception | None = None
        for attempt in range(self.settings.retry_count + 1):
            try:
                await asyncio.wait_for(
                    self._send_once(event, image_path),
                    timeout=self.settings.timeout_seconds,
                )
                return
            except Exception as exc:
                last_error = exc
                if attempt >= self.settings.retry_count:
                    raise
        if last_error is not None:
            raise last_error

    async def _send_once(self, event: Any, path: Path) -> None:
        mode = self.settings.mode
        if mode == "auto":
            mode = (
                "chain"
                if callable(getattr(event, "chain_result", None))
                and callable(getattr(event, "send", None))
                else "image_result"
            )
        if mode == "chain":
            chain_result = getattr(event, "chain_result", None)
            send = getattr(event, "send", None)
            if not callable(chain_result) or not callable(send):
                raise RuntimeError("AstrBot event does not support chain sending")
            result = chain_result([self._make_image(str(path))])
            await self._maybe_await(send(result))
            return
        image_result = getattr(event, "image_result", None)
        send = getattr(event, "send", None)
        if not callable(image_result) or not callable(send):
            raise RuntimeError("AstrBot event does not support image_result sending")
        await self._maybe_await(send(image_result(str(path))))

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    def _make_image(self, path: str) -> Any:
        factory = self._image_factory
        if factory is None:
            # Keep pure maintenance tools and tests independent of the optional
            # AstrBot runtime; the runtime import happens only on a real send.
            from astrbot.api.message_components import Image

            factory = Image.fromFileSystem
        return factory(path)


__all__ = ["MemeSender", "SendPipelineSettings"]
