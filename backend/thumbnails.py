"""Bounded asynchronous thumbnail loading without AstrBot dependencies."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import stat as stat_module
import warnings
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypeVar

try:
    from PIL import Image as PILImage
except ImportError:  # pragma: no cover - exercised only without the optional dependency
    PILImage = None


DEFAULT_MAX_DECODED_PIXELS = 25_000_000


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Portable fields that change when a file is replaced or rewritten."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class ThumbnailCacheKey:
    generation: int
    image_id: str
    thumbnail_size: int
    canonical_path: str
    identity: FileIdentity


@dataclass(frozen=True, slots=True)
class _RequestKey:
    generation: int
    image_id: str
    thumbnail_size: int
    lexical_path: str


@dataclass(frozen=True, slots=True)
class _InspectedFile:
    path: Path
    canonical_path: str
    identity: FileIdentity


Renderer = Callable[[Path, int], str]
LoadResult = TypeVar("LoadResult")


class ThumbnailManager:
    """Coalesce, bound, cache, and invalidate asynchronous thumbnail work.

    A small preliminary request key coalesces simultaneous calls before even a
    filesystem stat is queued.  The persistent LRU key is stronger: it includes
    the current index generation, canonical path, and a portable stat identity.
    """

    def __init__(
        self,
        renderer: Renderer,
        *,
        cache_capacity: int = 512,
        max_concurrency: int = 4,
        max_pending: int = 64,
    ) -> None:
        if cache_capacity < 1:
            raise ValueError("cache_capacity must be positive")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if max_pending < max_concurrency:
            raise ValueError("max_pending must be at least max_concurrency")
        self._renderer = renderer
        self._cache_capacity = cache_capacity
        self._max_pending = max_pending
        self._thread_slots = asyncio.Semaphore(max_concurrency)
        self._state_lock = asyncio.Lock()
        self._cache: OrderedDict[ThumbnailCacheKey, str] = OrderedDict()
        self._inflight: dict[_RequestKey, asyncio.Task[str]] = {}
        self._generation = 0
        self._closed = False

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def cache_count(self) -> int:
        return len(self._cache)

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    @property
    def cache_keys(self) -> tuple[ThumbnailCacheKey, ...]:
        """Expose immutable diagnostics useful to health checks and tests."""

        return tuple(self._cache)

    def mark_load_success(self) -> int:
        """Advance the index generation and invalidate all cached thumbnails."""

        if self._closed:
            return self._generation
        self._generation += 1
        self._cache.clear()
        return self._generation

    def run_index_load(self, loader: Callable[[], LoadResult]) -> LoadResult:
        """Run an index load and invalidate only after it returns successfully."""

        result = loader()
        self.mark_load_success()
        return result

    async def get_thumbnail(
        self, image_id: str, image_path: str | Path, thumbnail_size: int
    ) -> str:
        if not isinstance(image_id, str) or not image_id:
            return ""
        if (
            isinstance(thumbnail_size, bool)
            or not isinstance(thumbnail_size, int)
            or thumbnail_size < 1
        ):
            return ""
        try:
            lexical_path = os.path.normcase(
                os.path.abspath(os.fspath(image_path))
            )
        except (TypeError, ValueError, OSError):
            return ""

        async with self._state_lock:
            if self._closed:
                return ""
            request_key = _RequestKey(
                self._generation,
                image_id,
                thumbnail_size,
                lexical_path,
            )
            task = self._inflight.get(request_key)
            if task is None:
                if len(self._inflight) >= self._max_pending:
                    return ""
                task = asyncio.create_task(
                    self._process(request_key, Path(image_path)),
                    name=f"thumbnail:{image_id[:64]}",
                )
                self._inflight[request_key] = task

        try:
            # One disconnected HTTP client must not cancel work shared by others.
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if self._closed or task.cancelled():
                return ""
            raise

    async def _process(self, request_key: _RequestKey, path: Path) -> str:
        current_task = asyncio.current_task()
        try:
            inspected = await self._run_blocking(_inspect_file, path)
            if self._closed or request_key.generation != self._generation:
                return ""

            cache_key = ThumbnailCacheKey(
                generation=request_key.generation,
                image_id=request_key.image_id,
                thumbnail_size=request_key.thumbnail_size,
                canonical_path=inspected.canonical_path,
                identity=inspected.identity,
            )
            async with self._state_lock:
                if self._closed or request_key.generation != self._generation:
                    return ""
                if cache_key in self._cache:
                    cached = self._cache[cache_key]
                    self._cache.move_to_end(cache_key)
                    return cached

            rendered = await self._run_blocking(
                self._renderer,
                inspected.path,
                request_key.thumbnail_size,
            )
            data = rendered if isinstance(rendered, str) else ""
            async with self._state_lock:
                # A refresh may finish while Pillow is still decoding the old file.
                if self._closed or request_key.generation != self._generation:
                    return ""
                self._cache[cache_key] = data
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._cache_capacity:
                    self._cache.popitem(last=False)
            return data
        except Exception:
            return ""
        finally:
            async with self._state_lock:
                if self._inflight.get(request_key) is current_task:
                    self._inflight.pop(request_key, None)

    async def _run_blocking(self, function: Callable, *args):
        """Run one bounded thread job and do not release its slot prematurely.

        Cancelling ``asyncio.to_thread`` cannot stop the underlying thread.  When
        termination cancels an in-flight request, wait for an already-running
        worker before releasing the semaphore so the real concurrency bound holds.
        """

        async with self._thread_slots:
            worker = asyncio.create_task(asyncio.to_thread(function, *args))
            try:
                return await asyncio.shield(worker)
            except asyncio.CancelledError:
                try:
                    await worker
                except Exception:
                    pass
                raise

    async def close(self) -> None:
        """Reject new work, cancel queued work, and drain running thread jobs."""

        async with self._state_lock:
            if self._closed and not self._inflight:
                self._cache.clear()
                return
            self._closed = True
            self._generation += 1
            self._cache.clear()
            tasks = tuple(self._inflight.values())
            for task in tasks:
                task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._state_lock:
            self._inflight.clear()
            self._cache.clear()


def _inspect_file(path: Path) -> _InspectedFile:
    canonical = path.resolve(strict=True)
    stat_result = canonical.stat()
    if not stat_module.S_ISREG(stat_result.st_mode):
        raise OSError("thumbnail source is not a regular file")
    identity = FileIdentity(
        device=int(stat_result.st_dev),
        inode=int(stat_result.st_ino),
        size=int(stat_result.st_size),
        mtime_ns=int(stat_result.st_mtime_ns),
        ctime_ns=int(stat_result.st_ctime_ns),
    )
    return _InspectedFile(
        path=canonical,
        canonical_path=os.path.normcase(os.fspath(canonical)),
        identity=identity,
    )


def render_pillow_thumbnail(
    image_path: Path,
    thumbnail_size: int,
    *,
    max_decoded_pixels: int = DEFAULT_MAX_DECODED_PIXELS,
) -> str:
    """Render a JPEG thumbnail while refusing decompression-bomb dimensions."""

    if PILImage is None:
        return ""
    if (
        isinstance(thumbnail_size, bool)
        or not isinstance(thumbnail_size, int)
        or thumbnail_size < 1
        or isinstance(max_decoded_pixels, bool)
        or not isinstance(max_decoded_pixels, int)
        or max_decoded_pixels < 1
    ):
        return ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PILImage.DecompressionBombWarning)
            with PILImage.open(image_path) as image:
                width, height = image.size
                if width < 1 or height < 1 or width * height > max_decoded_pixels:
                    return ""
                image.draft("RGB", (thumbnail_size, thumbnail_size))
                image.thumbnail(
                    (thumbnail_size, thumbnail_size),
                    PILImage.Resampling.LANCZOS,
                )
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=60)
                return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:
        return ""


__all__ = [
    "DEFAULT_MAX_DECODED_PIXELS",
    "FileIdentity",
    "ThumbnailCacheKey",
    "ThumbnailManager",
    "render_pillow_thumbnail",
]
