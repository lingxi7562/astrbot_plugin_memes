import unittest

from backend.query import build_query_plan


class QueryPlanTests(unittest.TestCase):
    def test_one_sentence_intent_promotes_known_library_tags(self):
        plan = build_query_plan(
            intent="对方讲了冷笑话，我想无语吐槽",
            known_tags=["冷笑", "无语", "开心"],
        )
        self.assertFalse(plan.is_empty)
        self.assertEqual(plan.terms[0], "对方讲了冷笑话，我想无语吐槽")
        self.assertIn("冷笑", plan.terms)
        self.assertIn("无语", plan.terms)
        self.assertFalse(plan.used_context)

    def test_active_message_is_a_safe_fallback_when_arguments_are_omitted(self):
        plan = build_query_plan(
            context="给我发一个开心地打招呼的表情包",
            known_tags=["开心", "打招呼"],
        )
        self.assertTrue(plan.used_context)
        self.assertEqual(plan.terms[0], "给我发一个开心地打招呼的表情包")
        self.assertIn("开心", plan.terms)

    def test_legacy_tags_and_scene_are_deduplicated(self):
        plan = build_query_plan(
            tags=["生气", "生气", "吐槽"],
            scene="对方迟到了",
            known_tags=["生气", "吐槽", "迟到"],
        )
        self.assertEqual(len(plan.terms), len({term.casefold() for term in plan.terms}))
        self.assertIn("生气", plan.terms)
        self.assertIn("吐槽", plan.terms)
        self.assertIn("迟到", plan.terms)

    def test_malformed_empty_input_does_not_invent_a_mood(self):
        plan = build_query_plan(intent=None, tags=None, scene=None, context=None)
        self.assertTrue(plan.is_empty)
        self.assertEqual(plan.terms, ())

    def test_term_count_is_bounded(self):
        plan = build_query_plan(
            intent=" ".join(f"word-{number}" for number in range(100)),
            max_terms=3,
        )
        self.assertLessEqual(len(plan.terms), 3)


if __name__ == "__main__":
    unittest.main()
