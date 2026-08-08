"""Cloud-only API contract check for the minimum supported AstrBot release."""

from __future__ import annotations

import ast
import importlib.util
import re
import runpy
import sys
from pathlib import Path


EXPECTED_VERSION = "4.26.0"
REQUIRED_WEB_EXPORTS = {"error_response", "json_response", "request"}


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: check_astrbot_contract.py <AstrBot checkout>")
    repository = Path(__file__).resolve().parents[2]
    astrbot_checkout = Path(sys.argv[1]).resolve()

    version_module = runpy.run_path(str(astrbot_checkout / "astrbot" / "__init__.py"))
    if version_module.get("__version__") != EXPECTED_VERSION:
        fail("the checked-out AstrBot version is not the declared minimum")

    metadata = (repository / "metadata.yaml").read_text(encoding="utf-8")
    version_match = re.search(r'^astrbot_version:\s*["\']([^"\']+)["\']', metadata, re.M)
    if not version_match or version_match.group(1) != f">={EXPECTED_VERSION},<5":
        fail("metadata.yaml does not match the contract-tested AstrBot version")

    web_path = astrbot_checkout / "astrbot" / "api" / "web.py"
    spec = importlib.util.spec_from_file_location("_astrbot_api_web_contract", web_path)
    if spec is None or spec.loader is None:
        fail("unable to load the minimum AstrBot Web API module")
    web_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web_module)
    missing = sorted(name for name in REQUIRED_WEB_EXPORTS if not hasattr(web_module, name))
    if missing:
        fail(f"minimum AstrBot Web API is missing exports: {missing}")
    request_type = type(web_module.request)
    if not callable(getattr(request_type, "json", None)):
        fail("minimum AstrBot request proxy does not provide async JSON parsing")
    if not isinstance(getattr(request_type, "query", None), property):
        fail("minimum AstrBot request proxy does not provide query parameters")

    context_tree = ast.parse(
        (astrbot_checkout / "astrbot" / "core" / "star" / "context.py").read_text(
            encoding="utf-8"
        )
    )
    context_methods = {
        node.name
        for node in ast.walk(context_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if "register_web_api" not in context_methods:
        fail("minimum AstrBot Context does not provide register_web_api")

    plugin_tree = ast.parse((repository / "main.py").read_text(encoding="utf-8"))
    imported_web_names = {
        alias.name
        for node in ast.walk(plugin_tree)
        if isinstance(node, ast.ImportFrom) and node.module == "astrbot.api.web"
        for alias in node.names
    }
    unknown_imports = sorted(imported_web_names - REQUIRED_WEB_EXPORTS)
    if unknown_imports:
        fail(f"plugin imports unverified AstrBot Web API names: {unknown_imports}")

    print(f"AstrBot {EXPECTED_VERSION} API contract verified")


if __name__ == "__main__":
    main()
