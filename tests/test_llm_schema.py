import unittest

from backend.llm_schema import (
    request_meme_review_parameters,
    send_meme_parameters,
)


class LlmSchemaTests(unittest.TestCase):
    def test_intent_is_the_single_low_effort_entry_point(self):
        schema = send_meme_parameters()
        self.assertEqual(schema["required"], [])
        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(schema["properties"]["intent"]["type"], "string")
        self.assertLessEqual(schema["properties"]["intent"]["maxLength"], 256)

    def test_advanced_controls_are_bounded_and_optional(self):
        properties = send_meme_parameters()["properties"]
        self.assertLessEqual(properties["tags"]["maxItems"], 4)
        self.assertNotIn("tags", send_meme_parameters()["required"])
        self.assertNotIn("pack", send_meme_parameters()["required"])
        self.assertNotIn("persona", send_meme_parameters()["required"])

    def test_delegated_review_schema_is_single_optional_hint(self):
        schema = request_meme_review_parameters()
        self.assertEqual(schema["required"], [])
        self.assertEqual(set(schema["properties"]), {"hint"})
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["hint"]["maxLength"], 256)


if __name__ == "__main__":
    unittest.main()
