import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


if "backend" not in sys.modules:
    package = types.ModuleType("backend")
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "backend")]
    sys.modules["backend"] = package

from backend.index import (  # noqa: E402
    DirectorySource,
    JsonSource,
    LibraryLoadError,
    MemeIndex,
    SourceConfigurationError,
)


def write_index(path: Path, images: object) -> None:
    path.write_text(json.dumps({"images": images,}, ensure_ascii=False), "utf-8")


class MemeIndexTests(unittest.TestCase):
    def test_legacy_source_preserves_original_ids_and_matcher_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "smile.png").write_bytes(b"image")
            index_path = root / "index.json"
            write_index(
                index_path,
                {
                    "smile": {
                        "rel_path": "smile.png",
                        "tags": [" Happy ", "happy", "Cute"],
                    }
                },
            )
            index = MemeIndex(index_path, root)

            report = index.load()

            self.assertEqual(set(index.images), {"smile"})
            self.assertEqual(index.images["smile"]["tags"], ["happy", "cute"])
            self.assertEqual(index.tag_to_ids["happy"], ["smile"])
            self.assertEqual(index.get_abs_path(index.images["smile"]), root / "smile.png")
            self.assertTrue(report["committed"])

    def test_legacy_ids_remain_stable_when_an_additional_source_is_added(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "old.png").write_bytes(b"old")
            legacy_path = root / "legacy.json"
            write_index(legacy_path, {"old": {"rel_path": "old.png", "tags": []}})
            extra_root = root / "extra"
            extra_root.mkdir()
            (extra_root / "new.png").write_bytes(b"new")
            extra_path = extra_root / "index.json"
            write_index(extra_path, {"new": {"rel_path": "new.png", "tags": []}})
            index = MemeIndex(legacy_path, root)
            index.load()
            self.assertEqual(set(index.images), {"old"})

            index.add_json_source(
                {"path": extra_path, "root": extra_root, "namespace": "extra"}
            )
            index.load()

            self.assertEqual(set(index.images), {"old", "extra:new"})

    def test_atomic_rollback_on_invalid_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "safe.png").write_bytes(b"image")
            index_path = root / "index.json"
            write_index(index_path, {"safe": {"rel_path": "safe.png", "tags": ["safe"]}})
            index = MemeIndex(index_path, root)
            index.load()
            old_images = index.images
            old_tags = index.tag_to_ids
            write_index(
                index_path,
                {"unsafe": {"rel_path": "../escape.png", "tags": ["unsafe"]}},
            )

            with self.assertRaises(LibraryLoadError):
                index.load()

            self.assertIs(index.images, old_images)
            self.assertIs(index.tag_to_ids, old_tags)
            self.assertIn("safe", index.images)
            self.assertFalse(index.get_status_report()["committed"])

    def test_duplicate_namespaces_fail_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            (first / "one.png").write_bytes(b"one")
            (second / "two.png").write_bytes(b"two")
            first_index = first / "index.json"
            second_index = second / "index.json"
            write_index(first_index, {"one": {"rel_path": "one.png", "tags": []}})
            write_index(second_index, {"two": {"rel_path": "two.png", "tags": []}})
            index = MemeIndex(
                json_sources=[
                    {"path": first_index, "root": first, "namespace": "shared"},
                    {"path": second_index, "root": second, "namespace": "shared"},
                ]
            )

            with self.assertRaises(LibraryLoadError) as caught:
                index.load()

            self.assertEqual(index.count, 0)
            self.assertEqual(caught.exception.report["error_count"], 2)
            self.assertIn("duplicate source namespace", str(caught.exception))

    def test_duplicate_image_id_fails_instead_of_first_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one.png").write_bytes(b"one")
            (root / "two.png").write_bytes(b"two")
            index_path = root / "index.json"
            write_index(
                index_path,
                [
                    {"id": "same", "rel_path": "one.png", "tags": []},
                    {"id": "same", "rel_path": "two.png", "tags": []},
                ],
            )
            index = MemeIndex(json_sources=[{"path": index_path, "namespace": "lib"}])

            with self.assertRaises(LibraryLoadError) as caught:
                index.load()

            self.assertEqual(caught.exception.report["duplicate_count"], 1)
            self.assertIn("duplicate image id", str(caught.exception))

    def test_get_abs_path_ignores_caller_fields_and_rejects_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "safe.png").write_bytes(b"safe")
            index_path = root / "index.json"
            write_index(index_path, {"safe": {"rel_path": "safe.png", "tags": []}})
            index = MemeIndex(index_path, root)
            index.load()

            forged = {
                "id": "safe",
                "rel_path": "../escape.png",
                "_source_root": str(root.parent),
            }
            self.assertEqual(index.get_abs_path(forged), root / "safe.png")
            with self.assertRaises(SourceConfigurationError):
                index.get_abs_path({"id": "unknown", "rel_path": "safe.png"})

    @unittest.skipIf(os.name == "nt", "backslash is a path separator on Windows")
    def test_get_abs_path_preserves_posix_backslash_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            filename = "back\\slash.png"
            expected = root / filename
            expected.write_bytes(b"image")
            index = MemeIndex(
                directory_sources=[{"root": root, "namespace": "local"}]
            )

            index.load()
            item = index.images[f"local:{filename}"]

            self.assertEqual(index.get_abs_path(item), expected.resolve())

    @unittest.skipIf(os.name == "nt", "portable symlink setup is POSIX-only")
    def test_get_abs_path_rejects_source_root_replaced_by_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "source"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "safe.png").write_bytes(b"safe")
            (outside / "safe.png").write_bytes(b"outside")
            index_path = root / "index.json"
            write_index(index_path, {"safe": {"rel_path": "safe.png", "tags": []}})
            index = MemeIndex(index_path, root)
            index.load()
            item = index.images["safe"]

            parked = base / "parked"
            root.rename(parked)
            root.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(SourceConfigurationError):
                index.get_abs_path(item)

    def test_invalid_runtime_dataclass_is_reported_by_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bad = JsonSource(None, root, "bad")  # type: ignore[arg-type]
            index = MemeIndex(json_sources=[bad])

            with self.assertRaises(LibraryLoadError) as caught:
                index.load()

            self.assertEqual(caught.exception.report["sources"][0]["status"], "error")
            self.assertIn("path", str(caught.exception))

    def test_directory_dataclass_extensions_none_uses_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "image.png").write_bytes(b"image")
            source = DirectorySource(root, "local", extensions=None)  # type: ignore[arg-type]
            index = MemeIndex(directory_sources=[source])

            index.load()

            self.assertEqual(set(index.images), {"local:image.png"})

    def test_casefold_tags_are_used_for_storage_and_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "word.png").write_bytes(b"image")
            index_path = root / "index.json"
            write_index(
                index_path,
                {"word": {"rel_path": "word.png", "tags": ["Stra\u00dfe", "STRASSE"]}},
            )
            index = MemeIndex(index_path, root)

            index.load()

            self.assertEqual(index.images["word"]["tags"], ["strasse"])
            self.assertEqual(index.tag_to_ids["strasse"], ["word"])

    def test_missing_file_is_reported_as_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index_path = root / "index.json"
            write_index(index_path, {"missing": {"rel_path": "missing.png", "tags": []}})
            index = MemeIndex(index_path, root)

            report = index.load()

            self.assertEqual(index.count, 1)
            self.assertEqual(report["missing_file_count"], 1)
            self.assertEqual(report["sources"][0]["status"], "warning")

    def test_unified_sources_support_json_and_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            json_root = root / "json"
            directory_root = root / "folder"
            json_root.mkdir()
            directory_root.mkdir()
            (json_root / "indexed.png").write_bytes(b"image")
            (directory_root / "scanned.jpg").write_bytes(b"image")
            index_path = json_root / "index.json"
            write_index(index_path, {"item": {"rel_path": "indexed.png", "tags": []}})
            index = MemeIndex(
                sources=[
                    {
                        "type": "json",
                        "path": index_path,
                        "root": json_root,
                        "namespace": "jsonlib",
                    },
                    {"type": "directory", "root": directory_root, "namespace": "dirlib"},
                ],
                allowed_roots=[root],
            )

            index.load()

            self.assertEqual(set(index.images), {"jsonlib:item", "dirlib:scanned.jpg"})


if __name__ == "__main__":
    unittest.main()
