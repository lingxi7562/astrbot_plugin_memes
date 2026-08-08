import ast
import unittest
from pathlib import Path


class CommandContractTests(unittest.TestCase):
    def test_main_registers_grouped_meme_commands(self):
        tree = ast.parse(Path("main.py").read_text(encoding="utf-8"))
        event_imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "astrbot.api.event"
            for alias in node.names
        }
        self.assertTrue({"filter", "AstrMessageEvent"}.issubset(event_imports))
        decorators = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
                    if isinstance(decorator.func.value, ast.Name) and decorator.func.value.id in {"filter", "meme_commands"}:
                        if decorator.args and isinstance(decorator.args[0], ast.Constant):
                            decorators.append((decorator.func.attr, decorator.args[0].value))
        names = {value for kind, value in decorators if kind == "command_group"}
        commands = {value for kind, value in decorators if kind == "command"}
        self.assertIn("meme", names)
        self.assertTrue({"search", "send", "list", "refresh", "stats"}.issubset(commands))


if __name__ == "__main__":
    unittest.main()
