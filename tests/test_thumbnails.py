import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.thumbnails import (
    PILImage,
    ThumbnailManager,
    render_pillow_thumbnail,
)


class TrackingRenderer:
    def __init__(self, delay: float = 0.04):
        self.delay = delay
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, path: Path, size: int) -> str:
        with self._lock:
            self.calls += 1
            call = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            return f"render-{call}-{path.name}-{size}"
        finally:
            with self._lock:
                self.active -= 1


class BlockingRenderer:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def __call__(self, path: Path, size: int) -> str:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2)
        return f"render-{self.calls}"


class ThumbnailManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.image_path = Path(self.temp_dir.name) / "image.bin"
        self.image_path.write_bytes(b"first")

    async def test_same_key_shares_one_inflight_task(self):
        renderer = TrackingRenderer()
        manager = ThumbnailManager(renderer, max_concurrency=2, max_pending=8)
        self.addAsyncCleanup(manager.close)

        results = await asyncio.gather(
            *(manager.get_thumbnail("same", self.image_path, 200) for _ in range(20))
        )

        self.assertEqual(renderer.calls, 1)
        self.assertEqual(len(set(results)), 1)
        self.assertTrue(results[0])
        self.assertEqual(manager.inflight_count, 0)

    async def test_thread_concurrency_and_unique_pending_are_bounded(self):
        renderer = TrackingRenderer(delay=0.08)
        manager = ThumbnailManager(renderer, max_concurrency=2, max_pending=3)
        self.addAsyncCleanup(manager.close)

        results = await asyncio.gather(
            *(
                manager.get_thumbnail(f"image-{number}", self.image_path, 200)
                for number in range(12)
            )
        )

        self.assertEqual(renderer.calls, 3)
        self.assertLessEqual(renderer.max_active, 2)
        self.assertEqual(sum(bool(result) for result in results), 3)
        self.assertEqual(manager.inflight_count, 0)

    async def test_cache_key_uses_generation_canonical_path_and_stat_identity(self):
        renderer = TrackingRenderer(delay=0)
        manager = ThumbnailManager(renderer, cache_capacity=8)
        self.addAsyncCleanup(manager.close)

        first = await manager.get_thumbnail("identity", self.image_path, 160)
        cached = await manager.get_thumbnail("identity", self.image_path, 160)
        self.assertEqual(first, cached)
        self.assertEqual(renderer.calls, 1)

        key = manager.cache_keys[0]
        self.assertEqual(key.generation, 0)
        self.assertEqual(key.canonical_path, os.path.normcase(str(self.image_path.resolve())))
        self.assertEqual(key.identity.size, len(b"first"))
        self.assertEqual(key.thumbnail_size, 160)

        self.image_path.write_bytes(b"second-version")
        changed = await manager.get_thumbnail("identity", self.image_path, 160)
        self.assertNotEqual(changed, first)
        self.assertEqual(renderer.calls, 2)
        self.assertEqual(manager.cache_keys[-1].identity.size, len(b"second-version"))

    async def test_old_generation_cannot_return_or_refill_after_refresh(self):
        renderer = BlockingRenderer()
        manager = ThumbnailManager(renderer, max_concurrency=1, max_pending=4)
        self.addAsyncCleanup(manager.close)

        old_request = asyncio.create_task(
            manager.get_thumbnail("old", self.image_path, 200)
        )
        for _ in range(100):
            if renderer.started.is_set():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(renderer.started.is_set())

        self.assertEqual(manager.mark_load_success(), 1)
        renderer.release.set()
        self.assertEqual(await old_request, "")
        self.assertEqual(manager.cache_count, 0)

        fresh = await manager.get_thumbnail("old", self.image_path, 200)
        self.assertTrue(fresh)
        self.assertEqual(manager.cache_keys[0].generation, 1)

        generation = manager.generation
        cache_keys = manager.cache_keys

        def failed_load():
            raise RuntimeError("reload failed")

        with self.assertRaises(RuntimeError):
            manager.run_index_load(failed_load)
        self.assertEqual(manager.generation, generation)
        self.assertEqual(manager.cache_keys, cache_keys)

        report = {"committed": True}
        self.assertIs(manager.run_index_load(lambda: report), report)
        self.assertEqual(manager.generation, generation + 1)
        self.assertEqual(manager.cache_count, 0)

    async def test_close_cancels_queue_and_drains_running_thread(self):
        renderer = BlockingRenderer()
        manager = ThumbnailManager(renderer, max_concurrency=1, max_pending=4)
        first = asyncio.create_task(manager.get_thumbnail("first", self.image_path, 200))
        for _ in range(100):
            if renderer.started.is_set():
                break
            await asyncio.sleep(0.005)
        self.assertTrue(renderer.started.is_set())
        second = asyncio.create_task(
            manager.get_thumbnail("second", self.image_path, 200)
        )
        for _ in range(100):
            if manager.inflight_count == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(manager.inflight_count, 2)

        closing = asyncio.create_task(manager.close())
        await asyncio.sleep(0.02)
        self.assertFalse(closing.done())
        renderer.release.set()
        await closing

        self.assertEqual(await first, "")
        self.assertEqual(await second, "")
        self.assertEqual(manager.inflight_count, 0)
        self.assertEqual(manager.cache_count, 0)
        self.assertEqual(
            await manager.get_thumbnail("after-close", self.image_path, 200), ""
        )


@unittest.skipIf(PILImage is None, "Pillow is not installed")
class PillowRendererTests(unittest.TestCase):
    def test_pixel_limit_rejects_before_decode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "small.png"
            PILImage.new("RGB", (20, 20), "red").save(path)

            self.assertEqual(
                render_pillow_thumbnail(path, 10, max_decoded_pixels=399),
                "",
            )
            self.assertTrue(
                render_pillow_thumbnail(path, 10, max_decoded_pixels=400)
            )


if __name__ == "__main__":
    unittest.main()
