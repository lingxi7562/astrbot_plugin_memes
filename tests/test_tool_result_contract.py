import ast
import unittest
from pathlib import Path


class ToolResultContractTests(unittest.TestCase):
    def test_tool_uses_callable_call_tool_result_instead_of_union_alias(self):
        tree = ast.parse(Path("backend/tool.py").read_text(encoding="utf-8"))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ToolExecResult"
        ]
        self.assertFalse(calls)
        helper_names = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("_tool_result", helper_names)


if __name__ == "__main__":
    unittest.main()
