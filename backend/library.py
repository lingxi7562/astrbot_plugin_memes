"""Source loading and validation for the meme library.

This module intentionally depends only on the Python standard library.  It keeps
all filesystem and schema handling away from the matching code, and produces a
complete candidate snapshot which callers may commit atomically.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping, Sequence


DEFAULT_IMAGE_EXTENSIONS = frozenset(
    {".apng", ".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
)


class LibraryError(ValueError):
    """Base class for source, schema, and path validation errors."""


class SourceConfigurationError(LibraryError):
    """Raised when a source definition is incomplete or unsafe."""


class SourceSchemaError(LibraryError):
    """Raised when a JSON index does not conform to the supported schema."""


class UnsafePathError(LibraryError):
    """Raised when an image path is absolute or escapes its source root."""


class LibraryLoadError(LibraryError):
    """Raised when one or more sources fail to build a candidate snapshot."""

    def __init__(self, report: Mapping[str, Any]):
        self.report = dict(report)
        messages: list[str] = []
        for source in self.report.get("sources", []):
            for error in source.get("errors", []):
                messages.append(f"{source.get('namespace', '?')}: {error}")
        super().__init__("library build failed: " + "; ".join(messages))


@dataclass(frozen=True)
class JsonSource:
    """A JSON index plus the filesystem root used by its ``rel_path`` values."""

    path: Path
    root: Path
    namespace: str
    legacy_ids: bool = False


@dataclass(frozen=True)
class DirectorySource:
    """A local directory scanned for supported image files."""

    root: Path
    namespace: str
    recursive: bool = True
    extensions: frozenset[str] = DEFAULT_IMAGE_EXTENSIONS
    tags: tuple[str, ...] = ()


@dataclass
class SourceStatus:
    """JSON-serialisable diagnostics for one source build attempt."""

    namespace: str
    kind: str
    location: str
    status: str = "ok"
    count: int = 0
    errors: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)

    def finalise(self) -> None:
        if self.errors:
            self.status = "error"
        elif self.missing_files or self.duplicates:
            self.status = "warning"
        else:
            self.status = "ok"

    def as_dict(self) -> dict[str, Any]:
        self.finalise()
        return {
            "namespace": self.namespace,
            "kind": self.kind,
            "location": self.location,
            "status": self.status,
            "count": self.count,
            "errors": list(self.errors),
            "missing_files": list(self.missing_files),
            "duplicates": list(self.duplicates),
        }


@dataclass(frozen=True)
class LibrarySnapshot:
    """A fully validated, not-yet-committed library state."""

    images: dict[str, dict[str, Any]]
    resolved_paths: dict[str, Path]
    source_roots: frozenset[Path]
    statuses: tuple[SourceStatus, ...]


def normalise_namespace(value: Any, *, fallback_path: Path, kind: str) -> str:
    """Return a validated explicit namespace or a deterministic path namespace."""

    if value is None or (isinstance(value, str) and not value.strip()):
        # Namespace identity follows the host filesystem's case semantics.  In
        # particular, POSIX paths which differ only by case remain distinct.
        resolved = os.path.normcase(str(fallback_path.resolve(strict=False))).encode("utf-8")
        digest = hashlib.sha256(resolved).hexdigest()[:10]
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", fallback_path.stem).strip("-._")
        value = f"{kind}-{stem or 'source'}-{digest}"
    if not isinstance(value, str):
        raise SourceConfigurationError("source namespace must be a string")
    namespace = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", namespace):
        raise SourceConfigurationError(
            "source namespace must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}"
        )
    return namespace


def normalise_tags(value: Any, *, field_name: str = "tags") -> list[str]:
    """Validate and case-insensitively de-duplicate a tag list."""

    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise SourceSchemaError(f"{field_name} must be a list of strings")
    result: list[str] = []
    seen: set[str] = set()
    for position, tag in enumerate(value):
        if not isinstance(tag, str):
            raise SourceSchemaError(f"{field_name}[{position}] must be a string")
        cleaned = " ".join(tag.split()).strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key not in seen:
            seen.add(key)
            result.append(cleaned.casefold())
    return result


def resolve_relative_path(root: Path, rel_path: Any) -> tuple[str, Path]:
    """Validate ``rel_path`` and resolve it beneath ``root``.

    Both POSIX and Windows absolute/drive forms are rejected so indexes remain
    safe even when they are authored on a platform other than the host.
    """

    if not isinstance(rel_path, str) or not rel_path.strip():
        raise SourceSchemaError("rel_path must be a non-empty string")
    raw = rel_path.strip()
    if "\x00" in raw:
        raise UnsafePathError("rel_path contains a NUL byte")
    windows_path = PureWindowsPath(raw)
    # A backslash is a valid filename character on POSIX.  On Windows it is a
    # separator, so only that host converts it to the portable POSIX form.
    portable = raw.replace("\\", "/") if os.name == "nt" else raw
    posix_path = PurePosixPath(portable)
    if windows_path.is_absolute() or windows_path.drive or posix_path.is_absolute():
        raise UnsafePathError(f"absolute rel_path is not allowed: {raw!r}")

    canonical_root = root.resolve(strict=False)
    # Converting separators makes JSON indexes portable between Windows and POSIX.
    candidate = canonical_root.joinpath(*posix_path.parts).resolve(strict=False)
    if not _is_within(candidate, canonical_root):
        raise UnsafePathError(f"rel_path escapes source root: {raw!r}")
    try:
        canonical_rel = candidate.relative_to(canonical_root).as_posix()
    except ValueError as exc:  # Defensive; _is_within already checks this.
        raise UnsafePathError(f"rel_path escapes source root: {raw!r}") from exc
    if canonical_rel in ("", "."):
        raise SourceSchemaError("rel_path must identify a file")
    return canonical_rel, candidate


def build_snapshot(
    json_sources: Sequence[JsonSource | str | Path | Mapping[str, Any]],
    directory_sources: Sequence[DirectorySource | str | Path | Mapping[str, Any]],
    *,
    allowed_roots: Sequence[Path] = (),
) -> LibrarySnapshot:
    """Build a complete snapshot without mutating a live index."""

    images: dict[str, dict[str, Any]] = {}
    resolved_paths: dict[str, Path] = {}
    source_roots: set[Path] = set()
    statuses: list[SourceStatus] = []
    validated_json: list[tuple[JsonSource, SourceStatus]] = []
    validated_directories: list[tuple[DirectorySource, SourceStatus]] = []

    try:
        canonical_allowed = tuple(
            _path_value(path, "allowed root").resolve(strict=False)
            for path in allowed_roots
        )
    except Exception as exc:
        status = SourceStatus("configuration", "configuration", "allowed_roots")
        status.errors.append(str(exc))
        statuses.append(status)
        raise LibraryLoadError(make_report(statuses, 0, committed=False)) from exc

    # Reconstruct every source, including dataclass instances, before touching
    # the filesystem.  Thus malformed runtime dataclasses become ordinary load
    # diagnostics rather than leaking TypeError/AttributeError.
    for position, raw_source in enumerate(json_sources):
        status = SourceStatus(
            _safe_source_value(raw_source, "namespace", f"invalid-json-{position}"),
            "json",
            _safe_source_value(raw_source, "path", "<invalid>"),
        )
        statuses.append(status)
        try:
            source = json_source_from_value(raw_source)
            status.namespace = source.namespace
            status.location = str(source.path)
            validated_json.append((source, status))
        except Exception as exc:
            status.errors.append(str(exc))

    for position, raw_source in enumerate(directory_sources):
        status = SourceStatus(
            _safe_source_value(raw_source, "namespace", f"invalid-directory-{position}"),
            "directory",
            _safe_source_value(raw_source, "root", "<invalid>"),
        )
        statuses.append(status)
        try:
            source = directory_source_from_value(raw_source)
            status.namespace = source.namespace
            status.location = str(source.root)
            validated_directories.append((source, status))
        except Exception as exc:
            status.errors.append(str(exc))

    namespace_statuses: dict[str, list[SourceStatus]] = {}
    for source, status in [*validated_json, *validated_directories]:
        namespace_statuses.setdefault(source.namespace, []).append(status)
    for namespace, duplicates in namespace_statuses.items():
        if len(duplicates) <= 1:
            continue
        message = f"duplicate source namespace: {namespace!r}"
        for status in duplicates:
            status.errors.append(message)

    if any(status.errors for status in statuses):
        raise LibraryLoadError(make_report(statuses, 0, committed=False))

    for source, status in validated_json:
        try:
            root = _validate_source_root(source.root, canonical_allowed, must_exist=False)
            source_roots.add(root)
            entries = _load_json_entries(
                source.path,
                root,
                source.namespace,
                status,
                preserve_ids=source.legacy_ids,
            )
            _merge_entries(entries, images, resolved_paths, status)
        except (
            LibraryError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            AttributeError,
        ) as exc:
            status.errors.append(str(exc))
        status.finalise()

    for source, status in validated_directories:
        try:
            root = _validate_source_root(source.root, canonical_allowed, must_exist=True)
            source_roots.add(root)
            entries = _scan_directory(source, root)
            _merge_entries(entries, images, resolved_paths, status)
        except (LibraryError, OSError, UnicodeError, TypeError, AttributeError) as exc:
            status.errors.append(str(exc))
        status.finalise()

    report = make_report(statuses, len(images), committed=False)
    if any(status.errors for status in statuses):
        raise LibraryLoadError(report)
    return LibrarySnapshot(
        images=images,
        resolved_paths=resolved_paths,
        source_roots=frozenset(source_roots),
        statuses=tuple(statuses),
    )


def _safe_source_value(source: Any, field: str, fallback: str) -> str:
    try:
        if isinstance(source, Mapping):
            value = source.get(field, fallback)
        else:
            value = getattr(source, field, fallback)
        return str(value) if value is not None else fallback
    except Exception:
        return fallback


def make_report(
    statuses: Iterable[SourceStatus], count: int, *, committed: bool
) -> dict[str, Any]:
    source_dicts = [status.as_dict() for status in statuses]
    return {
        "status": "error" if any(s["status"] == "error" for s in source_dicts) else "ok",
        "committed": committed,
        "count": count,
        "source_count": len(source_dicts),
        "missing_file_count": sum(len(s["missing_files"]) for s in source_dicts),
        "duplicate_count": sum(len(s["duplicates"]) for s in source_dicts),
        "error_count": sum(len(s["errors"]) for s in source_dicts),
        "sources": source_dicts,
    }


def json_source_from_value(
    value: JsonSource | str | Path | Mapping[str, Any],
    *,
    default_root: Path | None = None,
    default_namespace: str | None = None,
    legacy_ids: bool = False,
) -> JsonSource:
    if isinstance(value, JsonSource):
        # Rebuild even already-instantiated dataclasses: runtime typing does not
        # prevent callers from constructing them with invalid field values.
        path = _path_value(value.path, "JSON source path")
        root = _path_value(value.root, "JSON source root")
        namespace_value: Any = value.namespace
        if not isinstance(value.legacy_ids, bool):
            raise SourceConfigurationError("JSON source legacy_ids must be a boolean")
        legacy_value = value.legacy_ids
    elif isinstance(value, (str, Path)):
        path = _path_value(value, "JSON source path")
        root = default_root if default_root is not None else path.parent
        namespace_value: Any = default_namespace
        legacy_value = legacy_ids
    elif isinstance(value, Mapping):
        raw_path = value.get("path", value.get("index_path"))
        path = _path_value(raw_path, "JSON source path/index_path")
        raw_root = value.get("root", value.get("data_root"))
        root = (
            _path_value(raw_root, "JSON source root/data_root")
            if raw_root is not None and str(raw_root).strip()
            else (default_root if default_root is not None else path.parent)
        )
        namespace_value = value.get("namespace", value.get("name", default_namespace))
        legacy_raw = value.get("legacy_ids", legacy_ids)
        if not isinstance(legacy_raw, bool):
            raise SourceConfigurationError("JSON source legacy_ids must be a boolean")
        legacy_value = legacy_raw
    else:
        raise SourceConfigurationError("JSON source must be a path, mapping, or JsonSource")
    root = _path_value(root, "JSON source root")
    namespace = normalise_namespace(namespace_value, fallback_path=path, kind="json")
    return JsonSource(path=path, root=root, namespace=namespace, legacy_ids=legacy_value)


def directory_source_from_value(
    value: DirectorySource | str | Path | Mapping[str, Any],
) -> DirectorySource:
    if isinstance(value, DirectorySource):
        root = _path_value(value.root, "directory source root")
        namespace_value: Any = value.namespace
        if not isinstance(value.recursive, bool):
            raise SourceConfigurationError("directory source recursive must be a boolean")
        recursive = value.recursive
        extensions = _normalise_extensions(value.extensions)
        tags = tuple(normalise_tags(value.tags, field_name="source tags"))
    elif isinstance(value, (str, Path)):
        root = _path_value(value, "directory source root")
        namespace_value: Any = None
        recursive = True
        extensions = DEFAULT_IMAGE_EXTENSIONS
        tags: tuple[str, ...] = ()
    elif isinstance(value, Mapping):
        raw_root = value.get("root", value.get("path"))
        root = _path_value(raw_root, "directory source root/path")
        namespace_value = value.get("namespace", value.get("name"))
        recursive_value = value.get("recursive", True)
        if not isinstance(recursive_value, bool):
            raise SourceConfigurationError("directory source recursive must be a boolean")
        recursive = recursive_value
        extensions = _normalise_extensions(value.get("extensions"))
        tags = tuple(normalise_tags(value.get("tags", []), field_name="source tags"))
    else:
        raise SourceConfigurationError(
            "directory source must be a path, mapping, or DirectorySource"
        )
    namespace = normalise_namespace(namespace_value, fallback_path=root, kind="dir")
    return DirectorySource(root, namespace, recursive, extensions, tags)


def _path_value(value: Any, field_name: str) -> Path:
    if not isinstance(value, (str, Path)) or isinstance(value, bool):
        raise SourceConfigurationError(f"{field_name} must be a path string")
    if not str(value).strip():
        raise SourceConfigurationError(f"{field_name} must not be empty")
    return Path(value)


def _normalise_extensions(value: Any) -> frozenset[str]:
    if value is None:
        return DEFAULT_IMAGE_EXTENSIONS
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise SourceConfigurationError("extensions must be a list of file suffixes")
    result: set[str] = set()
    for extension in value:
        if not isinstance(extension, str) or not extension.strip():
            raise SourceConfigurationError("each extension must be a non-empty string")
        cleaned = extension.strip().lower()
        if not cleaned.startswith("."):
            cleaned = "." + cleaned
        if not re.fullmatch(r"\.[a-z0-9]{1,12}", cleaned):
            raise SourceConfigurationError(f"invalid image extension: {extension!r}")
        result.add(cleaned)
    if not result:
        raise SourceConfigurationError("extensions must not be empty")
    return frozenset(result)


def _validate_source_root(
    root: Path, allowed_roots: Sequence[Path], *, must_exist: bool
) -> Path:
    canonical = root.resolve(strict=False)
    if must_exist and not canonical.exists():
        raise SourceConfigurationError(f"source directory does not exist: {canonical}")
    if canonical.exists() and not canonical.is_dir():
        raise SourceConfigurationError(f"source root is not a directory: {canonical}")
    if allowed_roots and not any(_is_within(canonical, allowed) for allowed in allowed_roots):
        raise SourceConfigurationError(
            f"source root is outside configured allowed_roots: {canonical}"
        )
    return canonical


def _load_json_entries(
    index_path: Path,
    root: Path,
    namespace: str,
    status: SourceStatus,
    *,
    preserve_ids: bool = False,
) -> list[tuple[str, dict[str, Any], Path]]:
    path = index_path.resolve(strict=False)
    if not path.is_file():
        raise SourceConfigurationError(f"JSON index does not exist or is not a file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle, object_pairs_hook=_reject_duplicate_json_keys)
    if not isinstance(data, dict):
        raise SourceSchemaError("JSON index root must be an object")
    if "images" not in data:
        raise SourceSchemaError("JSON index must contain an images field")
    raw_images = data["images"]
    rows: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(raw_images, dict):
        for local_id, item in raw_images.items():
            if not isinstance(local_id, str) or not local_id.strip():
                raise SourceSchemaError("each images object key must be a non-empty string")
            if not isinstance(item, dict):
                raise SourceSchemaError(f"image {local_id!r} must be an object")
            if "id" in item and item["id"] != local_id:
                raise SourceSchemaError(
                    f"image {local_id!r} has an id that does not match its object key"
                )
            rows.append((local_id, item))
    elif isinstance(raw_images, list):
        for position, item in enumerate(raw_images):
            if not isinstance(item, dict):
                raise SourceSchemaError(f"images[{position}] must be an object")
            local_id = item.get("id")
            if not isinstance(local_id, str) or not local_id.strip():
                raise SourceSchemaError(f"images[{position}].id must be a non-empty string")
            rows.append((local_id, item))
    else:
        raise SourceSchemaError("images must be an object or a list")

    entries: list[tuple[str, dict[str, Any], Path]] = []
    for local_id, item in rows:
        entry, resolved = _normalise_json_item(
            namespace, local_id, item, root, preserve_id=preserve_ids
        )
        if not resolved.is_file():
            status.missing_files.append(entry["rel_path"])
        entries.append((entry["id"], entry, resolved))
    return entries


def _normalise_json_item(
    namespace: str,
    local_id: str,
    item: Mapping[str, Any],
    root: Path,
    *,
    preserve_id: bool = False,
) -> tuple[dict[str, Any], Path]:
    local_id = local_id.strip()
    if not local_id or "\x00" in local_id:
        raise SourceSchemaError("image id must be a non-empty string without NUL bytes")
    rel_path, resolved = resolve_relative_path(root, item.get("rel_path"))
    filename_value = item.get("filename", Path(rel_path).name)
    if not isinstance(filename_value, str):
        raise SourceSchemaError(f"image {local_id!r} filename must be a string")
    filename = filename_value.strip() or Path(rel_path).name
    if "/" in filename or "\\" in filename:
        raise SourceSchemaError(f"image {local_id!r} filename must not contain a path")
    tags = normalise_tags(item.get("tags", []), field_name=f"image {local_id!r} tags")
    image_id = local_id if preserve_id else f"{namespace}:{local_id}"
    # Preserve non-reserved metadata while replacing all operational fields with
    # validated canonical values.
    entry = {
        key: value
        for key, value in item.items()
        if key not in {"id", "filename", "rel_path", "tags", "_source_root", "_source"}
    }
    entry.update(
        {
            "id": image_id,
            "source_id": local_id,
            "source": namespace,
            "filename": filename,
            "rel_path": rel_path,
            "tags": tags,
            "_source_root": str(root),
        }
    )
    return entry, resolved


def _scan_directory(
    source: DirectorySource, root: Path
) -> list[tuple[str, dict[str, Any], Path]]:
    iterator = root.rglob("*") if source.recursive else root.glob("*")
    candidates: list[tuple[str, Path]] = []
    for candidate in iterator:
        if not candidate.is_file() or candidate.suffix.lower() not in source.extensions:
            continue
        resolved = candidate.resolve(strict=True)
        if not _is_within(resolved, root):
            raise UnsafePathError(f"scanned file escapes source root: {candidate}")
        rel_path = resolved.relative_to(root).as_posix()
        candidates.append((rel_path, resolved))
    candidates.sort(key=lambda pair: pair[0].casefold())

    entries: list[tuple[str, dict[str, Any], Path]] = []
    for rel_path, resolved in candidates:
        path = PurePosixPath(rel_path)
        derived_tags: list[str] = list(source.tags)
        derived_tags.extend(part for part in path.parent.parts if part not in ("", "."))
        derived_tags.extend(part for part in re.split(r"[\s_.-]+", path.stem) if part)
        tags = normalise_tags(derived_tags)
        local_id = rel_path
        image_id = f"{source.namespace}:{local_id}"
        entry = {
            "id": image_id,
            "source_id": local_id,
            "source": source.namespace,
            "filename": path.name,
            "rel_path": rel_path,
            "tags": tags,
            "_source_root": str(root),
        }
        entries.append((image_id, entry, resolved))
    return entries


def _merge_entries(
    entries: Iterable[tuple[str, dict[str, Any], Path]],
    images: dict[str, dict[str, Any]],
    resolved_paths: dict[str, Path],
    status: SourceStatus,
) -> None:
    for image_id, entry, resolved in entries:
        if image_id in images:
            status.duplicates.append(image_id)
            status.errors.append(f"duplicate image id: {image_id!r}")
            continue
        images[image_id] = entry
        resolved_paths[image_id] = resolved
        status.count += 1


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceSchemaError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
