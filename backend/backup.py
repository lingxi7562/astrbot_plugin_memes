"""Validated managed-library backups and disaster-recovery snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


class BackupError(ValueError):
    """A safe backup or restore operation error."""


class BackupManager:
    IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
    NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.zip\Z")
    MAX_FILES = 10_000
    MAX_TOTAL_BYTES = 100 * 1024 * 1024
    MAX_ARCHIVE_BYTES = 120 * 1024 * 1024
    MAX_BACKUPS = 20

    def __init__(
        self,
        managed_root: str | Path,
        metadata_path: str | Path,
        backup_dir: str | Path,
        *,
        retention_count: int = MAX_BACKUPS,
    ) -> None:
        self.root = Path(managed_root)
        self.metadata_path = Path(metadata_path)
        self.backup_dir = Path(backup_dir)
        if isinstance(retention_count, bool) or not isinstance(retention_count, int):
            retention_count = self.MAX_BACKUPS
        self.retention_count = max(1, min(self.MAX_BACKUPS, retention_count))

    @staticmethod
    def _relative(value: str) -> str:
        if not isinstance(value, str) or not value or "\\" in value:
            raise BackupError("归档路径无效")
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or not path.parts
            or path.as_posix() == "."
            or any(part in {"", ".", ".."} for part in path.parts)
            or ":" in path.parts[0]
        ):
            raise BackupError("归档路径包含不安全跳转")
        return path.as_posix()

    @classmethod
    def _archive_name(cls, value: Any) -> str:
        if not isinstance(value, str) or not cls.NAME_PATTERN.fullmatch(value):
            raise BackupError("备份名称无效")
        return value

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _files_for_backup(self) -> list[tuple[str, Path]]:
        try:
            root = self.root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise BackupError("managed 目录不存在") from exc
        files: list[tuple[str, Path]] = []
        total = 0
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise BackupError("managed 目录包含不允许备份的符号链接")
            if not path.is_file() or path.suffix.casefold() not in self.IMAGE_EXTENSIONS:
                continue
            size = path.stat().st_size
            total += size
            if len(files) >= self.MAX_FILES or total > self.MAX_TOTAL_BYTES:
                raise BackupError("备份文件数量或总大小超出限制")
            files.append((f"library/{path.relative_to(root).as_posix()}", path))
        if self.metadata_path.is_file():
            size = self.metadata_path.stat().st_size
            total += size
            if total > self.MAX_TOTAL_BYTES:
                raise BackupError("备份总大小超出限制")
            files.append(("metadata/managed_metadata.json", self.metadata_path))
        return files

    def _cleanup_old_backups(self, protected: set[str] | None = None) -> None:
        protected = protected or set()
        backups = self.list_backups()
        for item in backups[self.retention_count :]:
            if item["name"] in protected:
                continue
            try:
                (self.backup_dir / item["name"]).unlink()
            except OSError:
                continue

    def create_snapshot(
        self, label: str = "snapshot", *, _protected_names: set[str] | None = None
    ) -> dict[str, Any]:
        files = self._files_for_backup()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_label = "".join(
            character for character in str(label) if character.isalnum() or character in "_.-"
        )[:32] or "snapshot"
        name = f"{timestamp}-{safe_label}-{uuid.uuid4().hex[:8]}.zip"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        final_path = self.backup_dir / name
        temporary: str | None = None
        manifest_files: list[dict[str, Any]] = []
        try:
            fd, temporary = tempfile.mkstemp(
                prefix=f".{name}.", suffix=".tmp", dir=self.backup_dir
            )
            os.close(fd)
            total = 0
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for archive_path, source in files:
                    size = source.stat().st_size
                    total += size
                    manifest_files.append(
                        {
                            "path": archive_path,
                            "size": size,
                            "sha256": self._sha256(source),
                        }
                    )
                    archive.write(source, archive_path)
                manifest = {
                    "version": 1,
                    "plugin": "astrbot_plugin_memes",
                    "created_at": timestamp,
                    "files": manifest_files,
                    "total_bytes": total,
                }
                archive.writestr(
                    "manifest.json",
                    json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
                )
            os.replace(temporary, final_path)
            temporary = None
            self._cleanup_old_backups(_protected_names)
            return {
                "name": name,
                "files": len(manifest_files),
                "total_bytes": total,
                "size": final_path.stat().st_size,
            }
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise BackupError("创建备份失败") from exc
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def list_backups(self) -> list[dict[str, Any]]:
        if not self.backup_dir.is_dir():
            return []
        result: list[dict[str, Any]] = []
        for path in self.backup_dir.iterdir():
            if not path.is_file() or not self.NAME_PATTERN.fullmatch(path.name):
                continue
            try:
                stat_result = path.stat()
                result.append(
                    {
                        "name": path.name,
                        "size": stat_result.st_size,
                        "modified": stat_result.st_mtime,
                    }
                )
            except OSError:
                continue
        result.sort(key=lambda item: item["modified"], reverse=True)
        return result[: self.MAX_BACKUPS]

    def _path_for_name(self, name: Any) -> Path:
        safe_name = self._archive_name(name)
        try:
            backup_root = self.backup_dir.resolve(strict=True)
            path = (backup_root / safe_name).resolve(strict=True)
            path.relative_to(backup_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise BackupError("备份不存在或路径无效") from exc
        if not path.is_file() or path.stat().st_size > self.MAX_ARCHIVE_BYTES:
            raise BackupError("备份不存在或大小超出限制")
        return path

    def _read_manifest(self, archive: zipfile.ZipFile) -> dict[str, Any]:
        try:
            manifest_info = archive.getinfo("manifest.json")
            if manifest_info.file_size > 1024 * 1024:
                raise BackupError("备份清单过大")
            raw = json.loads(archive.read("manifest.json"))
        except (KeyError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise BackupError("备份缺少有效清单") from exc
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise BackupError("备份版本不受支持")
        files = raw.get("files")
        if not isinstance(files, list) or len(files) > self.MAX_FILES:
            raise BackupError("备份清单文件数无效")
        total = 0
        for item in files:
            if not isinstance(item, dict):
                raise BackupError("备份清单格式无效")
            path = self._relative(item.get("path"))
            size = item.get("size")
            digest = item.get("sha256")
            if (
                not isinstance(size, int)
                or size < 0
                or size > self.MAX_TOTAL_BYTES
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest.casefold())
            ):
                raise BackupError("备份清单条目无效")
            if not (path.startswith("library/") or path == "metadata/managed_metadata.json"):
                raise BackupError("备份包含不允许的文件")
            if path.startswith("library/") and PurePosixPath(path).suffix.casefold() not in self.IMAGE_EXTENSIONS:
                raise BackupError("备份包含不支持的图片类型")
            total += size
        if total > self.MAX_TOTAL_BYTES:
            raise BackupError("备份解压总大小超出限制")
        return raw

    def validate_archive(self, name: Any) -> dict[str, Any]:
        path = self._path_for_name(name)
        try:
            with zipfile.ZipFile(path, "r") as archive:
                manifest = self._read_manifest(archive)
                names = {info.filename for info in archive.infolist() if not info.is_dir()}
                expected = {item["path"] for item in manifest["files"]} | {"manifest.json"}
                if names != expected:
                    raise BackupError("备份文件与清单不一致")
                return {
                    "name": path.name,
                    "files": len(manifest["files"]),
                    "total_bytes": manifest.get("total_bytes", 0),
                    "valid": True,
                }
        except zipfile.BadZipFile as exc:
            raise BackupError("备份不是有效 ZIP 文件") from exc

    def restore_snapshot(self, name: Any) -> dict[str, Any]:
        archive_path = self._path_for_name(name)
        self.validate_archive(name)
        recovery = self.create_snapshot(
            "pre-restore", _protected_names={archive_path.name}
        )
        temporary_dir: str | None = None
        try:
            temporary_dir = tempfile.mkdtemp(prefix=".restore-", dir=self.backup_dir)
            stage = Path(temporary_dir)
            with zipfile.ZipFile(archive_path, "r") as archive:
                manifest = self._read_manifest(archive)
                info_by_name = {info.filename: info for info in archive.infolist()}
                for item in manifest["files"]:
                    relative = item["path"]
                    info = info_by_name.get(relative)
                    if info is None or info.is_dir() or info.file_size != item["size"]:
                        raise BackupError("备份内容与清单不一致")
                    destination = (stage / relative).resolve()
                    destination.relative_to(stage.resolve())
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    with archive.open(info, "r") as source, destination.open("wb") as target:
                        copied = 0
                        while chunk := source.read(1024 * 1024):
                            copied += len(chunk)
                            if copied > self.MAX_TOTAL_BYTES:
                                raise BackupError("备份解压单项超出限制")
                            digest.update(chunk)
                            target.write(chunk)
                    if digest.hexdigest() != item["sha256"]:
                        raise BackupError("备份校验和不匹配")

            root = self.root.resolve(strict=True)
            for path in sorted(root.rglob("*"), reverse=True):
                if path.is_symlink():
                    raise BackupError("managed 目录包含符号链接，拒绝恢复")
                if path.is_file() and path.suffix.casefold() in self.IMAGE_EXTENSIONS:
                    path.unlink()
            for staged in sorted((stage / "library").rglob("*")) if (stage / "library").exists() else []:
                if not staged.is_file():
                    continue
                relative = staged.relative_to(stage / "library")
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staged, destination)
            staged_metadata = stage / "metadata/managed_metadata.json"
            if staged_metadata.is_file():
                self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staged_metadata, self.metadata_path)
            else:
                self.metadata_path.unlink(missing_ok=True)
            return {"restored": len(manifest["files"]), "recovery": recovery["name"]}
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            if isinstance(exc, BackupError):
                raise
            raise BackupError(
                f"恢复失败；恢复前快照为 {recovery['name']}"
            ) from exc
        finally:
            if temporary_dir:
                shutil.rmtree(temporary_dir, ignore_errors=True)

    def delete_backup(self, name: Any) -> None:
        path = self._path_for_name(name)
        try:
            path.unlink()
        except OSError as exc:
            raise BackupError("删除备份失败") from exc


__all__ = ["BackupError", "BackupManager"]
