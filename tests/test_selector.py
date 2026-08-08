import random
import tempfile
import unittest
from pathlib import Path

from backend.selector import MemeSelector, SelectionSettings


def candidate(image_id: str, path: Path, score: float) -> dict:
    return {"id": image_id, "path": str(path), "score": score}


class SelectorTests(unittest.TestCase):
    def test_duplicate_content_keeps_highest_scoring_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.png"
            second = root / "second.png"
            first.write_bytes(b"same image")
            second.write_bytes(b"same image")
            selector = MemeSelector(rng=random.Random(1))

            selected = selector.choose(
                [candidate("low", first, 1.0), candidate("high", second, 2.0)],
                settings=SelectionSettings.safe(mode="top"),
            )

            self.assertEqual(selected["id"], "high")
            self.assertEqual(selector.hash_cache_count, 2)

    def test_cooldown_is_scoped_and_avoids_recent_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = []
            for name in ("a.png", "b.png", "c.png"):
                path = root / name
                path.write_bytes(name.encode())
                paths.append(path)
            candidates = [
                candidate("a", paths[0], 3.0),
                candidate("b", paths[1], 2.0),
                candidate("c", paths[2], 1.0),
            ]
            selector = MemeSelector(rng=random.Random(1))
            settings = SelectionSettings.safe(
                mode="top", cooldown_seconds=100, history_size=10
            )

            first = selector.choose(candidates, scope="qq:group:1", settings=settings, now=0)
            second = selector.choose(candidates, scope="qq:group:1", settings=settings, now=1)
            third = selector.choose(candidates, scope="qq:group:1", settings=settings, now=2)
            other_scope = selector.choose(
                candidates, scope="qq:group:2", settings=settings, now=1
            )

            self.assertEqual([first["id"], second["id"], third["id"]], ["a", "b", "c"])
            self.assertEqual(other_scope["id"], "a")

    def test_single_candidate_can_repeat_only_when_pool_is_exhausted(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "only.png"
            path.write_bytes(b"only")
            selector = MemeSelector()
            settings = SelectionSettings.safe(mode="top", cooldown_seconds=100)

            first = selector.choose([candidate("one", path, 1)], settings=settings, now=0)
            second = selector.choose([candidate("one", path, 1)], settings=settings, now=1)

            self.assertEqual(first["id"], second["id"])

    def test_state_and_hash_cache_are_bounded_and_release_undoes_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selector = MemeSelector(max_scopes=2, max_hash_entries=1)
            settings = SelectionSettings.safe(mode="top")
            candidates = []
            for number in range(3):
                path = root / f"{number}.png"
                path.write_bytes(str(number).encode())
                candidates.append(candidate(str(number), path, 1))
                selector.choose(candidates[-1:], scope=f"scope-{number}", settings=settings)

            self.assertLessEqual(selector.scope_count, 2)
            self.assertLessEqual(selector.hash_cache_count, 1)
            selector.release(candidates[-1], scope="scope-2")
            self.assertEqual(selector.status()["recent_count"], 1)

    def test_release_only_removes_the_newest_matching_reservation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "one.png"
            path.write_bytes(b"one")
            selector = MemeSelector()
            settings = SelectionSettings.safe(mode="top", cooldown_seconds=100)
            item = candidate("one", path, 1)

            selector.choose([item], settings=settings, now=0)
            selector.choose([item], settings=settings, now=1)
            selector.release(item)

            self.assertEqual(selector.status()["recent_count"], 1)


if __name__ == "__main__":
    unittest.main()
