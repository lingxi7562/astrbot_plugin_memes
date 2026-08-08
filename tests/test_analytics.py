import json
import tempfile
import unittest
from pathlib import Path

from backend.analytics import AnalyticsSettings, MemeAnalytics


class AnalyticsTests(unittest.TestCase):
    def test_send_and_feedback_persist_without_raw_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "analytics.json"
            analytics = MemeAnalytics(path, clock=lambda: 100.0)
            analytics.record_send("qq:group:secret-user", "managed:one", ["开心"])
            analytics.record_failure("qq:group:secret-user", "managed:one")
            self.assertTrue(
                analytics.record_feedback(
                    "qq:group:secret-user", "managed:one", 1, ["开心"]
                )
            )

            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-user", raw)
            restored = MemeAnalytics(path, clock=lambda: 100.0)
            report = restored.report()
            self.assertEqual(report["totals"]["sends"], 1)
            self.assertEqual(report["totals"]["failures"], 1)
            self.assertEqual(report["totals"]["feedback"], 1)
            self.assertEqual(report["top_images"][0]["positive"], 1)

    def test_feedback_changes_personalized_selection_score_without_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            analytics = MemeAnalytics(Path(temporary) / "analytics.json")
            analytics.record_feedback("scope", "a", 1, ["happy"])
            analytics.record_feedback("scope", "b", -1, ["sad"])
            candidates = [
                {"id": "a", "score": 1.0, "tags": ["happy"]},
                {"id": "b", "score": 1.0, "tags": ["sad"]},
            ]

            ranked = analytics.personalize(candidates, scope="scope")

            self.assertGreater(ranked[0]["selection_score"], ranked[1]["selection_score"])
            self.assertNotIn("selection_score", candidates[0])

    def test_global_feedback_is_available_to_other_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            analytics = MemeAnalytics(Path(temporary) / "analytics.json")
            analytics.record_feedback("global", "a", 1, ["happy"])
            ranked = analytics.personalize(
                [
                    {"id": "a", "score": 1.0, "tags": ["happy"]},
                    {"id": "b", "score": 1.0, "tags": ["neutral"]},
                ],
                scope="qq:group:1",
            )
            self.assertGreater(ranked[0]["selection_score"], ranked[1]["selection_score"])

    def test_state_is_bounded_and_retention_prunes_old_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            now = [0.0]
            analytics = MemeAnalytics(
                Path(temporary) / "analytics.json",
                AnalyticsSettings.safe(retention_days=1),
                clock=lambda: now[0],
            )
            analytics.record_send("old", "old", [])
            now[0] = 2 * 86400
            analytics.record_send("new", "new", [])
            report = analytics.report()
            self.assertEqual(report["tracked_scopes"], 1)
            self.assertEqual(report["top_images"][0]["id"], "new")

    def test_corrupt_state_and_disabled_mode_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "analytics.json"
            path.write_text("not-json", encoding="utf-8")
            analytics = MemeAnalytics(path)
            analytics.record_send("scope", "image", [])
            self.assertEqual(analytics.report()["totals"]["sends"], 1)

            disabled = MemeAnalytics(
                Path(temporary) / "disabled.json",
                AnalyticsSettings.safe(enabled=False),
            )
            disabled.record_send("scope", "image", [])
            self.assertEqual(disabled.report()["totals"]["sends"], 0)
            self.assertFalse(disabled.record_feedback("scope", "image", 1, []))

    def test_reset_removes_persisted_aggregates(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "analytics.json"
            analytics = MemeAnalytics(path)
            analytics.record_send("scope", "image", [])
            self.assertTrue(analytics.reset())
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["totals"]["sends"], 0)
            self.assertEqual(analytics.report()["tracked_images"], 0)


if __name__ == "__main__":
    unittest.main()
