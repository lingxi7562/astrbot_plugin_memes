"""Safe managed-library metadata and import/delete helpers for the Page UI."""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any


class CatalogError(ValueError):
    """A user-facing managed-library operation error."""


class ManagedCatalog:
    MAX_IMPORT_BYTES = 10 * 1024 * 1024
    MAX_TAGS = 32
    MAX_TAG_LENGTH = 128
    MAX_METADATA_BYTES = 4 * 1024 * 1024
    MAX_METADATA_ENTRIES = 10_000
    ALLOWED_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

    def __init__(self, managed_root: str | Path, metadata_path: str | Path) -> None:
        self.root = Path(managed_root)
        self.metadata_path = Path(metadata_path)
        self._lock = RLock()
        self._overrides: dict[str, list[str]] = {}
        self._load()

    @staticmethod
    def _tags(values: Any) -> list[str]:
        if not isinstance(values, (list, tuple, set, frozenset)):
            raise CatalogError("tags 必须是列表")
        result: list[str] = []
        seen: set[str] = set()
        if len(values) > ManagedCatalog.MAX_TAGS:
            raise CatalogError("tags 数量超出限制")
        for value in values:
            if not isinstance(value, str):
                raise CatalogError("tags 必须全部是字符串")
            tag = " ".join(value.split()).strip()
            if not tag or len(tag) > ManagedCatalog.MAX_TAG_LENGTH:
                raise CatalogError("tag 格式无效")
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                result.append(tag)
        return result

    @classmethod
    def validate_tags(cls, values: Any) -> list[str]:
        return cls._tags(values)

    @staticmethod
    def _relative_key(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > 1024:
            raise CatalogError("rel_path 无效")
        path = PurePosixPath(value.replace("\\", "/"))
        if (
            path.is_absolute()
            or not path.parts
            or path.as_posix() == "."
            or any(part in {"", ".", ".."} for part in path.parts)
            or ":" in path.parts[0]
        ):
            raise CatalogError("rel_path 不允许跳转")
        return path.as_posix()

    def _load(self) -> None:
        try:
            if not self.metadata_path.is_file() or self.metadata_path.stat().st_size > self.MAX_METADATA_BYTES:
                return
            raw = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping) or raw.get("version") != 1:
                return
            overrides = raw.get("overrides")
            if not isinstance(overrides, Mapping):
                return
            for raw_path, raw_tags in list(overrides.items())[: self.MAX_METADATA_ENTRIES]:
                try:
                    path = self._relative_key(raw_path)
                    self._overrides[path] = self._tags(raw_tags)
                except CatalogError:
                    continue
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._overrides = {}

    def _persist_locked(self) -> None:
        temporary: str | None = None
        try:
            self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.metadata_path.name}.",
                suffix=".tmp",
                dir=self.metadata_path.parent,
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {"version": 1, "overrides": self._overrides},
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.metadata_path)
            temporary = None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def apply(self, index: Any) -> None:
        """Merge manual tags into live managed items and rebuild tag indexes."""

        with self._lock:
            for item in getattr(index, "images", {}).values():
                if not isinstance(item, dict) or item.get("source") != "managed":
                    continue
                rel_path = item.get("rel_path")
                if not isinstance(rel_path, str):
                    continue
                try:
                    key = self._relative_key(rel_path)
                except CatalogError:
                    continue
                extra = self._overrides.get(key, [])
                tags = item.get("tags", [])
                merged: list[str] = []
                seen: set[str] = set()
                for value in list(tags) + list(extra) if isinstance(tags, list) else extra:
                    if isinstance(value, str) and value.casefold() not in seen:
                        seen.add(value.casefold())
                        merged.append(value)
                item["tags"] = merged
            rebuild = getattr(index, "_build_inverted_index", None)
            if callable(rebuild):
                rebuild()

    def set_tags(self, rel_path: Any, tags: Any) -> list[str]:
        key = self._relative_key(rel_path)
        parsed = self._tags(tags)
        with self._lock:
            if len(self._overrides) >= self.MAX_METADATA_ENTRIES and key not in self._overrides:
                raise CatalogError("元数据条目已达到上限")
            if parsed:
                self._overrides[key] = parsed
            else:
                self._overrides.pop(key, None)
            self._persist_locked()
        return parsed

    def remove_metadata(self, rel_path: Any) -> None:
        key = self._relative_key(rel_path)
        with self._lock:
            self._overrides.pop(key, None)
            self._persist_locked()

    def import_base64(self, filename: Any, encoded: Any, tags: Any = ()) -> dict[str, Any]:
        if not isinstance(filename, str) or not filename.strip() or len(filename) > 256:
            raise CatalogError("filename 无效")
        original = Path(filename.strip()).name
        extension = Path(original).suffix.casefold()
        if original != filename.strip() or extension not in self.ALLOWED_EXTENSIONS:
            raise CatalogError("只支持 PNG、JPEG、GIF、WebP 文件名")
        if not isinstance(encoded, str) or len(encoded) > self.MAX_IMPORT_BYTES * 2:
            raise CatalogError("导入内容过大")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CatalogError("data_b64 不是有效的 Base64") from exc
        if not content or len(content) > self.MAX_IMPORT_BYTES:
            raise CatalogError("导入内容大小超出限制")
        if not self._looks_like_image(extension, content):
            raise CatalogError("文件内容与扩展名不匹配")
        parsed_tags = self._tags(tags)
        safe_name = f"{uuid.uuid4().hex}{extension}"
        destination = self.root / safe_name
        temporary: str | None = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".import-", suffix=extension, dir=self.root)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, destination)
            temporary = None
            rel_path = safe_name
            if parsed_tags:
                with self._lock:
                    if len(self._overrides) >= self.MAX_METADATA_ENTRIES:
                        destination.unlink(missing_ok=True)
                        raise CatalogError("元数据条目已达到上限")
                    self._overrides[rel_path] = parsed_tags
                    try:
                        self._persist_locked()
                    except (OSError, TypeError, ValueError) as exc:
                        self._overrides.pop(rel_path, None)
                        destination.unlink(missing_ok=True)
                        raise CatalogError("保存导入元数据失败") from exc
            return {"filename": safe_name, "rel_path": rel_path, "bytes": len(content)}
        except OSError as exc:
            raise CatalogError("写入 managed 目录失败") from exc
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @staticmethod
    def _looks_like_image(extension: str, content: bytes) -> bool:
        if extension == ".png":
            return content.startswith(b"\x89PNG\r\n\x1a\n")
        if extension in {".jpg", ".jpeg"}:
            return content.startswith(b"\xff\xd8\xff")
        if extension == ".gif":
            return content.startswith((b"GIF87a", b"GIF89a"))
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"

    def delete_path(self, rel_path: Any) -> None:
        key = self._relative_key(rel_path)
        raw_candidate = self.root / key
        if raw_candidate.is_symlink():
            raise CatalogError("不允许删除符号链接")
        try:
            candidate = raw_candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise CatalogError("文件不存在或路径无效") from exc
        root = self.root.resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise CatalogError("只能删除 managed 目录内的文件") from exc
        if not candidate.is_file() or candidate.is_symlink():
            raise CatalogError("目标不是普通文件")
        try:
            candidate.unlink()
        except OSError as exc:
            raise CatalogError("删除文件失败") from exc
        self.remove_metadata(key)


__all__ = ["CatalogError", "ManagedCatalog"]
