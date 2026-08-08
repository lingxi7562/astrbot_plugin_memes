import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from backend.backup import BackupError, BackupManager


PNG = b"\x89PNG\r\n\x1a\n" + b"payload"


class BackupTests(unittest.TestCase):
    def test_create_list_validate_and_restore_with_recovery_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            backups = Path(temporary) / "backups"
            root.mkdir()
            (root / "one.png").write_bytes(PNG)
            metadata = Path(temporary) / "managed_metadata.json"
            metadata.write_text(
                json.dumps({"version": 1, "overrides": {"one.png": ["tag"]}}),
                encoding="utf-8",
            )
            manager = BackupManager(root, metadata, backups)

            created = manager.create_snapshot("manual")
            self.assertTrue(manager.validate_archive(created["name"])["valid"])
            (root / "one.png").write_bytes(b"changed")
            restored = manager.restore_snapshot(created["name"])

            self.assertEqual((root / "one.png").read_bytes(), PNG)
            self.assertTrue(metadata.is_file())
            self.assertIn(restored["recovery"], {item["name"] for item in manager.list_backups()})

    def test_archive_path_traversal_and_tamper_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            backups = Path(temporary) / "backups"
            root.mkdir()
            manager = BackupManager(root, Path(temporary) / "metadata.json", backups)
            created = manager.create_snapshot()
            archive = backups / created["name"]
            tampered = backups / "tampered.zip"
            tampered.write_bytes(archive.read_bytes())
            with zipfile.ZipFile(tampered, "a") as handle:
                handle.writestr("../escape.txt", b"bad")
            with self.assertRaises(BackupError):
                manager.validate_archive("tampered.zip")
            with self.assertRaises(BackupError):
                manager.validate_archive("../tampered.zip")

    def test_retention_and_invalid_restore_do_not_leave_temp_dirs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "library"
            backups = Path(temporary) / "backups"
            root.mkdir()
            manager = BackupManager(
                root,
                Path(temporary) / "metadata.json",
                backups,
                retention_count=1,
            )
            first = manager.create_snapshot()
            second = manager.create_snapshot()
            self.assertEqual(len(manager.list_backups()), 1)
            self.assertNotEqual(first["name"], second["name"])
            with self.assertRaises(BackupError):
                manager.restore_snapshot("missing.zip")
            self.assertEqual(
                [path.name for path in backups.iterdir() if path.name.startswith(".restore-")],
                [],
            )


if __name__ == "__main__":
    unittest.main()
