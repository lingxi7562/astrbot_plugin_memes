import ast
import json
import unittest
from pathlib import Path


class EmotionAgentContractTests(unittest.TestCase):
    def test_main_keeps_private_send_tool_in_delegated_mode(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("EmotionDelegationTool.create", source)
        self.assertIn('self.meme_agent_mode == "emotion_agent"', source)
        self.assertIn("self.context.add_llm_tools(self.emotion_tool)", source)
        self.assertIn("self.context.add_llm_tools(self.meme_tool)", source)

    def test_bridge_uses_bounded_agent_tool_loop(self):
        tree = ast.parse(Path("backend/emotion_agent.py").read_text(encoding="utf-8"))
        source = Path("backend/emotion_agent.py").read_text(encoding="utf-8")
        classes = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.ClassDef,))
        }
        self.assertIn("EmotionAgentSettings", classes)
        self.assertIn("EmotionDelegationTool", classes)
        self.assertIn("_SingleUseMemeTool.from_tool(runtime.meme_tool)", source)
        self.assertIn("ToolSet([private_meme_tool])", source)
        self.assertIn("tool_loop_agent", source)
        self.assertIn("contexts=conversation_messages or None", source)
        self.assertIn("asyncio.wait_for", source)
        self.assertIn("send_meme", source)

    def test_schema_exposes_delegated_settings(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["meme_agent_mode"]["options"], ["direct", "emotion_agent"])
        self.assertEqual(schema["emotion_provider_id"]["_special"], "select_provider")
        self.assertIn("emotion_max_steps", schema)
        self.assertIn("emotion_timeout_seconds", schema)


if __name__ == "__main__":
    unittest.main()
