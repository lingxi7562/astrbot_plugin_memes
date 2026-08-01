import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# Importing backend normally also imports the AstrBot-only tool module.  Tests for
# this standard-library data layer use a lightweight package shell instead.
if "backend" not in sys.modules:
    package = types.ModuleType("backend")
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "backend")]
    sys.modules["backend"] = package

from backend.library import (  # noqa: E402
    DirectorySource,
    JsonSource,
    LibraryLoadError,
    SourceSchemaError,
    UnsafePathError,
    build_snapshot,
    normalise_namespace,
    normalise_tags,
    resolve_relative_path,
)


class PathValidationTests(unittest.TestCase):
    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            with self.assertRaises(UnsafePathError):
                resolve_relative_path(root, "../outside.png")

    def test_rejects_windows_and_posix_absolute_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for value in ("C:\\escape.png", "/escape.png", "\\\\server\\share\\x.png"):
                with self.subTest(value=value), self.assertRaises(UnsafePathError):
                    resolve_relative_path(root, value)

    def test_strict_tag_schema_and_deduplication(self):
        self.assertEqual(normalise_tags([" Happy ", "happy", "FUN"]), ["happy", "fun"])
        with self.assertRaises(SourceSchemaError):
            normalise_tags("not-a-list")
        with self.assertRaises(SourceSchemaError):
            normalise_tags(["ok", 3])

    def test_default_namespace_uses_host_normcase(self):
        with tempfile.TemporaryDirectory() as temporary:
            source_path = Path(temporary) / "Index.JSON"
            with patch("backend.library.os.path.normcase", wraps=os.path.normcase) as normcase:
                first = normalise_namespace(None, fallback_path=source_path, kind="json")
                second = normalise_namespace(None, fallback_path=source_path, kind="json")

            self.assertEqual(first, second)
            self.assertGreaterEqual(normcase.call_count, 2)

    @unittest.skipIf(os.name == "nt", "backslash is a separator on Windows")
    def test_posix_backslash_filename_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            filename = "literal\\name.png"
            (root / filename).write_bytes(b"image")

            rel_path, resolved = resolve_relative_path(root, filename)

            self.assertEqual(rel_path, filename)
            self.assertEqual(resolved, (root / filename).resolve())


class SnapshotBuildTests(unittest.TestCase):
    def test_bad_json_schema_reports_source_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index_path = root / "bad.json"
            index_path.write_text(json.dumps({"images": {"x": {"tags": "bad"}}}), "utf-8")
            source = JsonSource(index_path, root, "bad")

            with self.assertRaises(LibraryLoadError) as caught:
                build_snapshot([source], [])

            report = caught.exception.report
            self.assertFalse(report["committed"])
            self.assertEqual(report["error_count"], 1)
            self.assertEqual(report["sources"][0]["status"], "error")

    def test_directory_scan_is_stable_and_reports_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "memes"
            nested = root / "Cats"
            nested.mkdir(parents=True)
            (nested / "Very-Happy.PNG").write_bytes(b"image")
            (nested / "ignore.txt").write_text("no", "utf-8")

            snapshot = build_snapshot([], [DirectorySource(root, "local")])

            self.assertEqual(list(snapshot.images), ["local:Cats/Very-Happy.PNG"])
            item = snapshot.images["local:Cats/Very-Happy.PNG"]
            self.assertEqual(item["rel_path"], "Cats/Very-Happy.PNG")
            self.assertEqual(item["tags"], ["cats", "very", "happy"])
            self.assertEqual(snapshot.statuses[0].count, 1)

    def test_allowed_roots_rejects_uncontrolled_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            allowed = base / "allowed"
            outside = base / "outside"
            allowed.mkdir()
            outside.mkdir()

            with self.assertRaises(LibraryLoadError) as caught:
                build_snapshot(
                    [], [DirectorySource(outside, "outside")], allowed_roots=[allowed]
                )

            self.assertIn("outside configured allowed_roots", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
