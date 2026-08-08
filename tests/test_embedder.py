import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path

from backend.embedder import EmbeddingUnavailable, MemeEmbedder


class FakeProvider:
    def __init__(self, model: str = "fake-v1"):
        self.model = model
        self.batch_calls = 0
        self.query_calls = 0

    def meta(self):
        return types.SimpleNamespace(id="fake", type="test", model=self.model)

    def get_dim(self):
        return 3

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.casefold()
        if "feline" in lowered or "cat" in lowered:
            return [1.0, 0.0, 0.0]
        if "laugh" in lowered or "joy" in lowered:
            return [0.0, 1.0, 0.0]
        return [0.0, 0.0, 1.0]

    async def get_embeddings(self, texts: list[str]):
        self.batch_calls += 1
        await asyncio.sleep(0)
        return [self._vector(text) for text in texts]

    async def get_embedding(self, text: str):
        self.query_calls += 1
        await asyncio.sleep(0)
        return self._vector(text)


class FakeIndex:
    def __init__(self):
        self.images = {
            "cat": {
                "id": "cat",
                "filename": "cat.png",
                "source": "test",
                "rel_path": "cat.png",
                "tags": ["cat"],
            },
            "joy": {
                "id": "joy",
                "filename": "joy.png",
                "source": "test",
                "rel_path": "joy.png",
                "tags": ["joy"],
            },
        }


class MemeEmbedderTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_library_semantic_rank_does_not_require_keyword_recall(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = FakeProvider()
            embedder = MemeEmbedder(
                FakeIndex(), provider, Path(temporary) / "embedding_cache.json"
            )

            ranked = await embedder.rank_all(["feline"], limit=2)

            self.assertEqual(ranked[0][0], "cat")
            self.assertEqual(provider.batch_calls, 1)
            self.assertEqual(provider.query_calls, 1)

    async def test_cache_reuses_vectors_and_invalidates_changed_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "embedding_cache.json"
            first_provider = FakeProvider()
            first = MemeEmbedder(FakeIndex(), first_provider, cache)
            await first.ensure_index()
            self.assertTrue(cache.is_file())
            self.assertEqual(first_provider.batch_calls, 1)

            second_provider = FakeProvider()
            second = MemeEmbedder(FakeIndex(), second_provider, cache)
            await second.ensure_index()
            self.assertEqual(second_provider.batch_calls, 0)

            changed_index = FakeIndex()
            changed_index.images["joy"]["tags"] = ["laugh"]
            third_provider = FakeProvider()
            third = MemeEmbedder(changed_index, third_provider, cache)
            await third.ensure_index()
            self.assertEqual(third_provider.batch_calls, 1)
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])

    async def test_provider_fingerprint_and_malformed_cache_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "embedding_cache.json"
            cache.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "provider_key": "wrong",
                        "dimension": 3,
                        "items": {},
                    }
                ),
                encoding="utf-8",
            )
            provider = FakeProvider()
            embedder = MemeEmbedder(FakeIndex(), provider, cache)
            await embedder.ensure_index()
            self.assertEqual(provider.batch_calls, 1)

    async def test_dimension_change_fails_closed_without_writing_partial_cache(self):
        class BadProvider(FakeProvider):
            def get_dim(self):
                return 2

            async def get_embeddings(self, texts):
                self.batch_calls += 1
                return [[1.0, 0.0, 0.0] for _ in texts]

        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "embedding_cache.json"
            with self.assertRaises(EmbeddingUnavailable):
                await MemeEmbedder(FakeIndex(), BadProvider(), cache).ensure_index()
            self.assertFalse(cache.exists())
            self.assertEqual(list(Path(temporary).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
