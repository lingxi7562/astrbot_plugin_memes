"""Safe managed-library metadata and import/delete helpers for the Page UI."""

from __future__ import annotations

import base64
import binascii
import io
import json
import os
import stat
import tempfile
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any


class CatalogError(ValueError):
    """A user-facing managed-library operation error."""


class ManagedCatalog:
    MAX_IMPORT_BYTES = 10 * 1024 * 1024
    MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
    MAX_ARCHIVE_ENTRIES = 512
    MAX_ARCHIVE_FILES = 256
    MAX_ARCHIVE_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
    MAX_ARCHIVE_PATH_LENGTH = 512
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

    def import_archive_base64(
        self, filename: Any, encoded: Any, tags: Any = ()
    ) -> dict[str, Any]:
        """Import supported images from a ZIP archive into the managed library.

        Archive paths are normalised to portable POSIX paths.  When every image
        is below the same top-level directory (the common layout produced by
        most pack tools), that directory is removed so the resulting managed
        paths remain stable and useful as tags.  Existing files are never
        overwritten; a short unique suffix is added on collision.
        """

        if not isinstance(filename, str) or not filename.strip() or len(filename) > 256:
            raise CatalogError("压缩包文件名无效")
        archive_name = Path(filename.strip()).name
        if archive_name != filename.strip() or Path(archive_name).suffix.casefold() != ".zip":
            raise CatalogError("只支持 ZIP 压缩包")
        if not isinstance(encoded, str) or len(encoded) > (self.MAX_ARCHIVE_BYTES * 4 // 3) + 8:
            raise CatalogError("压缩包内容过大")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise CatalogError("archive_b64 不是有效的 Base64") from exc
        if not content or len(content) > self.MAX_ARCHIVE_BYTES:
            raise CatalogError("压缩包大小超出限制")
        parsed_tags = self._tags(tags)

        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except (OSError, zipfile.BadZipFile) as exc:
            raise CatalogError("压缩包无效或已损坏") from exc

        created: list[Path] = []
        temporary: str | None = None
        try:
            infos = archive.infolist()
            if len(infos) > self.MAX_ARCHIVE_ENTRIES:
                raise CatalogError("压缩包条目数量超出限制")
            image_entries: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            total_size = 0
            for info in infos:
                member_path = self._archive_member_path(info.filename)
                if info.flag_bits & 0x1:
                    raise CatalogError("不支持加密压缩包")
                mode = (info.external_attr >> 16) & 0xFFFF
                if stat.S_ISLNK(mode):
                    raise CatalogError("压缩包包含不安全的符号链接")
                if info.is_dir() or info.filename.replace("\\", "/").endswith("/"):
                    continue
                if info.file_size < 0 or info.file_size > self.MAX_IMPORT_BYTES:
                    raise CatalogError("压缩包内单个文件过大")
                total_size += info.file_size
                if total_size > self.MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise CatalogError("压缩包解压后总大小超出限制")
                extension = Path(member_path.name).suffix.casefold()
                if extension not in self.ALLOWED_EXTENSIONS:
                    continue
                if "__macosx" in {part.casefold() for part in member_path.parts}:
                    continue
                image_entries.append((info, member_path))
                if len(image_entries) > self.MAX_ARCHIVE_FILES:
                    raise CatalogError("压缩包内图片数量超出限制")

            if not image_entries:
                raise CatalogError("压缩包内没有受支持的图片")
            common_root = self._common_archive_root(
                [member_path for _, member_path in image_entries]
            )

            with self._lock:
                if parsed_tags and len(self._overrides) + len(image_entries) > self.MAX_METADATA_ENTRIES:
                    raise CatalogError("元数据条目已达到上限")
                self.root.mkdir(parents=True, exist_ok=True)
                canonical_root = self.root.resolve(strict=False)
                reserved: set[str] = set()
                imported: list[dict[str, Any]] = []
                actual_total = 0
                for info, member_path in image_entries:
                    relative = (
                        member_path.relative_to(common_root)
                        if common_root is not None
                        else member_path
                    )
                    rel_path = self._unique_archive_path(
                        relative.as_posix(), reserved, canonical_root
                    )
                    destination = self._safe_destination(rel_path, canonical_root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    parent = destination.parent.resolve(strict=False)
                    if not self._is_within_path(parent, canonical_root):
                        raise CatalogError("压缩包路径超出 managed 目录")

                    fd, temporary = tempfile.mkstemp(
                        prefix=".archive-",
                        suffix=Path(rel_path).suffix.casefold(),
                        dir=self.root,
                    )
                    written = 0
                    try:
                        with os.fdopen(fd, "wb") as handle, archive.open(info, "r") as source:
                            prefix = b""
                            while True:
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                if not prefix:
                                    prefix = chunk[:64]
                                written += len(chunk)
                                actual_total += len(chunk)
                                if written > self.MAX_IMPORT_BYTES:
                                    raise CatalogError("压缩包内单个文件过大")
                                if actual_total > self.MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                                    raise CatalogError("压缩包解压后总大小超出限制")
                                handle.write(chunk)
                            if not self._looks_like_image(
                                Path(rel_path).suffix.casefold(), prefix
                            ):
                                raise CatalogError("压缩包内存在内容与扩展名不匹配的图片")
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temporary, destination)
                        temporary = None
                    finally:
                        if temporary:
                            try:
                                os.unlink(temporary)
                            except OSError:
                                pass

                    created.append(destination)
                    reserved.add(rel_path)
                    imported.append(
                        {
                            "filename": destination.name,
                            "rel_path": rel_path,
                            "archive_path": member_path.as_posix(),
                            "bytes": written,
                        }
                    )

                if parsed_tags:
                    previous = dict(self._overrides)
                    try:
                        for item in imported:
                            self._overrides[item["rel_path"]] = list(parsed_tags)
                        self._persist_locked()
                    except (OSError, TypeError, ValueError) as exc:
                        self._overrides = previous
                        raise CatalogError("保存导入元数据失败") from exc

            return {
                "filename": archive_name,
                "files": imported,
                "count": len(imported),
                "bytes": sum(item["bytes"] for item in imported),
                "stripped_root": common_root.as_posix() if common_root else "",
            }
        except CatalogError:
            for path in reversed(created):
                try:
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
                except OSError:
                    pass
            raise
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            for path in reversed(created):
                try:
                    if path.is_file() and not path.is_symlink():
                        path.unlink()
                except OSError:
                    pass
            raise CatalogError("解压压缩包失败") from exc
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            try:
                archive.close()
            except OSError:
                pass

    @classmethod
    def _archive_member_path(cls, value: Any) -> PurePosixPath:
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or len(value) > cls.MAX_ARCHIVE_PATH_LENGTH
        ):
            raise CatalogError("压缩包内路径无效")
        raw = value.replace("\\", "/")
        windows = PurePosixPath(raw)
        if (
            windows.is_absolute()
            or not windows.parts
            or any(part in {"", ".", ".."} for part in windows.parts)
            or PurePosixPath(raw).drive
            or any(":" in part for part in windows.parts)
        ):
            raise CatalogError("压缩包内路径不安全")
        return windows

    @staticmethod
    def _common_archive_root(paths: list[PurePosixPath]) -> PurePosixPath | None:
        if not paths or any(len(path.parts) < 2 for path in paths):
            return None
        first = paths[0].parts[0]
        if not first or any(path.parts[0] != first for path in paths):
            return None
        return PurePosixPath(first)

    def _unique_archive_path(
        self, value: str, reserved: set[str], canonical_root: Path
    ) -> str:
        base = self._relative_key(value)
        candidate = base
        path = PurePosixPath(base)
        for _ in range(1000):
            candidate_path = self._safe_destination(candidate, canonical_root)
            if candidate not in reserved and not (
                candidate_path.exists() or candidate_path.is_symlink()
            ):
                return candidate
            stem = path.stem or "image"
            suffix = path.suffix
            name = f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"
            candidate = (path.parent / name).as_posix()
        raise CatalogError("无法为导入图片分配安全路径")

    def _safe_destination(self, rel_path: str, canonical_root: Path) -> Path:
        destination = canonical_root.joinpath(*PurePosixPath(rel_path).parts)
        parent = destination.parent.resolve(strict=False)
        if not self._is_within_path(parent, canonical_root):
            raise CatalogError("目标路径超出 managed 目录")
        return destination

    @staticmethod
    def _is_within_path(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

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
