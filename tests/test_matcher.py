import asyncio
import sys
import tempfile
import types
import unittest
from pathlib import Path

from backend.embedder import MemeEmbedder
from backend.matcher import TagMatcher


class SemanticProvider:
    def meta(self):
        return types.SimpleNamespace(id="semantic", type="test", model="v1")

    def get_dim(self):
        return 2

    @staticmethod
    def vector(text: str):
        if "feline" in text.casefold() or "cat" in text.casefold():
            return [1.0, 0.0]
        return [0.0, 1.0]

    async def get_embeddings(self, texts):
        await asyncio.sleep(0)
        return [self.vector(text) for text in texts]

    async def get_embedding(self, text):
        await asyncio.sleep(0)
        return self.vector(text)


class MatcherIndex:
    def __init__(self, root: Path):
        self.images = {
            "cat": {
                "id": "cat",
                "filename": "cat.png",
                "rel_path": "cat.png",
                "tags": ["cat"],
            },
            "laugh": {
                "id": "laugh",
                "filename": "laugh.png",
                "rel_path": "laugh.png",
                "tags": ["laugh"],
            },
        }
        self.tag_to_ids = {"cat": ["cat"], "laugh": ["laugh"]}
        self.root = root

    def get_abs_path(self, item):
        return self.root / item["rel_path"]


class MatcherSemanticTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedding_mode_returns_semantic_only_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = MatcherIndex(root)
            embedder = MemeEmbedder(index, SemanticProvider())
            matcher = TagMatcher(index)
            matcher.set_embedder(embedder)

            results = await matcher.match_embedding(["feline"], limit=1)

            self.assertEqual([result["id"] for result in results], ["cat"])
            self.assertEqual(results[0]["matched_tags"], ["feline"])

    async def test_hybrid_mode_keeps_semantic_only_candidates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index = MatcherIndex(root)
            embedder = MemeEmbedder(index, SemanticProvider())
            matcher = TagMatcher(index)
            matcher.set_embedder(embedder)

            results = await matcher.match_hybrid(["feline"], limit=2)

            self.assertEqual(results[0]["id"], "cat")
            self.assertEqual({result["id"] for result in results}, {"cat", "laugh"})


if __name__ == "__main__":
    unittest.main()
