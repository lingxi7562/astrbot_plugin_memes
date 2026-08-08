import ast
import json
import unittest
from pathlib import Path


class SenderContractTests(unittest.TestCase):
    def test_main_uses_instance_scoped_sender_pipeline(self):
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertIn("SendPipelineSettings.safe", source)
        self.assertIn("MemeSender(self.pipeline_settings)", source)
        self.assertIn("SendMemeTool.create", source)
        self.assertIn("self.context.add_llm_tools(self.meme_tool)", source)
        self.assertIn("await self.sender.send(event, Path(best[\"path\"]))", source)
        self.assertIn('f"/{PLUGIN_NAME}/pipeline"', source)
        tool_source = Path("backend/tool.py").read_text(encoding="utf-8")
        self.assertIn("ClassVar", tool_source)
        self.assertIn("runtime: Any", tool_source)

    def test_sender_has_no_eager_astrbot_import(self):
        tree = ast.parse(Path("backend/sender.py").read_text(encoding="utf-8"))
        top_level_imports = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        self.assertFalse(
            any(
                isinstance(node, ast.ImportFrom)
                and isinstance(node.module, str)
                and node.module.startswith("astrbot")
                for node in top_level_imports
            )
        )

    def test_schema_and_validator_expose_bounded_settings(self):
        schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
        for key in ("send_mode", "send_timeout_seconds", "send_retry_count"):
            self.assertIn(key, schema)
        validation = Path("web_validation.py").read_text(encoding="utf-8")
        self.assertIn('"send_mode"', validation)
        self.assertIn('"send_retry_count"', validation)


if __name__ == "__main__":
    unittest.main()
