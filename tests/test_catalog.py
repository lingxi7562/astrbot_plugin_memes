import base64
import json
import tempfile
import unittest
from pathlib import Path

from backend.catalog import CatalogError, ManagedCatalog


PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


class FakeIndex:
    def __init__(self, root: Path, rel_path: str = "one.png"):
        self.root = root
        self.images = {
            "managed:one": {
                "id": "managed:one",
                "source": "managed",
                "rel_path": rel_path,
                "tags": ["auto"],
            },
            "external:one": {
                "id": "external:one",
                "source": "external",
                "rel_path": rel_path,
                "tags": ["external"],
            },
        }
        self.rebuilt = 0

    def get_abs_path(self, item):
        return self.root / item["rel_path"]

    def _build_inverted_index(self):
        self.rebuilt += 1


class CatalogTests(unittest.TestCase):
    def test_import_is_validated_and_metadata_is_applied_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            catalog = ManagedCatalog(root, Path(temporary) / "managed_metadata.json")
            result = catalog.import_base64(
                "hello.png", base64.b64encode(PNG).decode(), ["greeting"]
            )
            self.assertTrue((root / result["filename"]).is_file())
            index = FakeIndex(root, result["rel_path"])
            catalog.apply(index)
            self.assertIn("greeting", index.images["managed:one"]["tags"])
            self.assertGreaterEqual(index.rebuilt, 1)

            with self.assertRaises(CatalogError):
                catalog.import_base64("evil.svg", base64.b64encode(b"<svg/>").decode())

    def test_tags_are_bounded_and_external_items_are_not_editable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            root.mkdir()
            (root / "one.png").write_bytes(PNG)
            catalog = ManagedCatalog(root, Path(temporary) / "managed_metadata.json")
            index = FakeIndex(root)
            index.images.pop("external:one")
            catalog.apply(index)
            tags = catalog.set_tags("one.png", ["manual"])
            self.assertEqual(tags, ["manual"])
            self.assertEqual(json.loads(catalog.metadata_path.read_text())["version"], 1)
            with self.assertRaises(CatalogError):
                catalog.set_tags("../outside.png", [])

    def test_delete_rejects_symlink_and_deletes_only_managed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            root.mkdir()
            (root / "one.png").write_bytes(PNG)
            catalog = ManagedCatalog(root, Path(temporary) / "managed_metadata.json")
            catalog.delete_path("one.png")
            self.assertFalse((root / "one.png").exists())
            with self.assertRaises(CatalogError):
                catalog.delete_path("one.png")

    def test_corrupt_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata = Path(temporary) / "managed_metadata.json"
            metadata.write_text("not-json", encoding="utf-8")
            catalog = ManagedCatalog(Path(temporary) / "library", metadata)
            self.assertEqual(catalog._overrides, {})


if __name__ == "__main__":
    unittest.main()
