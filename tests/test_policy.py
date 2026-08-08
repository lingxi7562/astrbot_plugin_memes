import tempfile
import unittest
from pathlib import Path

from backend.policy import MemePolicy, PolicySettings


def candidate(root: Path, image_id: str, tags=None, source="managed") -> dict:
    path = root / f"{image_id.replace(':', '_')}.png"
    path.write_bytes(b"image")
    return {"id": image_id, "path": str(path), "tags": tags or [], "source": source}


class PolicyTests(unittest.TestCase):
    def test_quota_reservation_and_specific_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = PolicySettings.safe(quota_window_seconds=100, quota_max_sends=2)
            policy = MemePolicy(settings, clock=lambda: 1.0)
            first = policy.reserve([candidate(root, "one")], scope="scope")
            second = policy.reserve([candidate(root, "two")], scope="scope")
            denied = policy.reserve([candidate(root, "three")], scope="scope")

            self.assertTrue(first.allowed)
            self.assertTrue(second.allowed)
            self.assertFalse(denied.allowed)
            policy.release("scope", first.reservation_id)
            allowed = policy.reserve([candidate(root, "three")], scope="scope")
            self.assertTrue(allowed.allowed)

    def test_content_governance_filters_and_blocks_query(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = PolicySettings.safe(
                blocked_tags=["nsfw"],
                allowed_tags=["safe"],
                blocked_namespaces=["legacy"],
                blocked_ids=["managed:blocked"],
            )
            policy = MemePolicy(settings, clock=lambda: 1.0)
            safe = candidate(root, "managed:safe", ["safe"])
            blocked = candidate(root, "managed:blocked", ["safe"])
            legacy = candidate(root, "legacy:old", ["safe"], source="legacy")
            denied = candidate(root, "managed:nsfw", ["nsfw"])

            selected = policy.reserve([safe, blocked, legacy, denied], scope="scope")
            query_denied = policy.reserve([safe], scope="other", query_tags=["nsfw"])

            self.assertTrue(selected.allowed)
            self.assertEqual([item["id"] for item in selected.candidates], ["managed:safe"])
            self.assertFalse(query_denied.allowed)
            self.assertEqual(query_denied.reason, "blocked_query_tag")

    def test_file_size_and_state_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "large.png"
            path.write_bytes(b"0" * 2048)
            policy = MemePolicy(PolicySettings.safe(max_file_bytes=5), clock=lambda: 1.0)
            item = {"id": "large", "path": str(path), "tags": []}
            self.assertFalse(policy.reserve([item], scope="one").allowed)
            bounded = MemePolicy(
                PolicySettings.safe(max_file_bytes=1024), max_scopes=1, clock=lambda: 1.0
            )
            small_path = root / "small.png"
            small_path.write_bytes(b"small")
            small = {"id": "small", "path": str(small_path), "tags": []}
            self.assertTrue(bounded.reserve([small], scope="one").allowed)
            self.assertTrue(bounded.reserve([small], scope="two").allowed)
            self.assertEqual(bounded.status()["tracked_scopes"], 1)

    def test_disabled_policy_does_not_filter(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = candidate(root, "managed:one", ["blocked"])
            policy = MemePolicy(PolicySettings.safe(enabled=False))
            decision = policy.reserve([item], query_tags=["blocked"])
            self.assertTrue(decision.allowed)
            self.assertEqual(decision.reason, "disabled")


if __name__ == "__main__":
    unittest.main()
