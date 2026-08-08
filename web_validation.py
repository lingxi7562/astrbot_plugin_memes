"""Pure validation helpers for the plugin Page Web APIs."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping


CONFIG_KEYS = frozenset(
    {
        "match_mode",
        "embedding_provider_id",
        "embedding_fallback",
        "max_match_candidates",
        "min_tag_score",
        "selection_mode",
        "selection_pool_size",
        "selection_cooldown_seconds",
        "selection_history_size",
        "deduplicate_files",
        "analytics_enabled",
        "analytics_retention_days",
        "personalization_strength",
        "auto_refresh",
        "thumbnail_size",
        "library_sources",
    }
)
MATCH_MODES = frozenset({"keyword", "embedding", "hybrid"})
SELECTION_MODES = frozenset({"weighted", "top", "random"})
LIST_SORTS = frozenset(
    {
        "id",
        "id_desc",
        "filename",
        "filename_desc",
        "tag_count",
        "tag_count_desc",
    }
)
LIBRARY_SOURCE_TEMPLATES = frozenset({"json", "directory"})
MAX_LIBRARY_SOURCES = 32
MAX_SOURCE_TAGS = 64
_NAMESPACE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ValidationError(ValueError):
    """A validation failure whose message is safe to return to a client."""


@dataclass(frozen=True, slots=True)
class ListQuery:
    page: int = 1
    page_size: int = 50
    q: str = ""
    tag: str = ""
    sort: str = "filename"


def _validate_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValidationError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return value


def _validate_number(
    value: Any, field: str, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} 必须是数字")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValidationError(f"{field} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return number


def validate_config_payload(
    payload: Any, available_provider_ids: Iterable[str]
) -> dict[str, Any]:
    """Validate a partial Page configuration update without coercing types."""

    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    unknown_keys = set(payload) - CONFIG_KEYS
    if unknown_keys:
        raise ValidationError("请求包含不支持的配置项")

    provider_ids = {provider_id for provider_id in available_provider_ids if provider_id}
    validated: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "match_mode":
            if not isinstance(value, str) or value not in MATCH_MODES:
                raise ValidationError("match_mode 无效")
            validated[key] = value
        elif key == "embedding_provider_id":
            if not isinstance(value, str):
                raise ValidationError("embedding_provider_id 必须是字符串")
            if value and value not in provider_ids:
                raise ValidationError("embedding_provider_id 不存在")
            validated[key] = value
        elif key in {"embedding_fallback", "auto_refresh", "analytics_enabled"}:
            if not isinstance(value, bool):
                raise ValidationError(f"{key} 必须是布尔值")
            validated[key] = value
        elif key == "max_match_candidates":
            validated[key] = _validate_int(value, key, 1, 100)
        elif key == "min_tag_score":
            validated[key] = _validate_number(value, key, 0.0, 100.0)
        elif key == "selection_mode":
            if not isinstance(value, str) or value not in SELECTION_MODES:
                raise ValidationError("selection_mode 无效")
            validated[key] = value
        elif key == "selection_pool_size":
            validated[key] = _validate_int(value, key, 1, 100)
        elif key == "selection_cooldown_seconds":
            validated[key] = _validate_number(value, key, 0.0, 30 * 24 * 3600.0)
        elif key == "selection_history_size":
            validated[key] = _validate_int(value, key, 1, 1000)
        elif key == "deduplicate_files":
            if not isinstance(value, bool):
                raise ValidationError(f"{key} 必须是布尔值")
            validated[key] = value
        elif key == "analytics_retention_days":
            validated[key] = _validate_int(value, key, 1, 365)
        elif key == "personalization_strength":
            validated[key] = _validate_number(value, key, 0.0, 2.0)
        elif key == "thumbnail_size":
            validated[key] = _validate_int(value, key, 50, 400)
        elif key == "library_sources":
            validated[key] = parse_library_sources(value)

    return validated


def _validate_source_path(value: Any, field: str) -> str:
    """Validate an explicit, cross-platform absolute source path.

    Source paths are administrator configuration, but accepting relative paths
    would make their meaning depend on AstrBot's working directory.  Rejecting
    traversal and control characters also ensures malformed template values never
    reach filesystem APIs or appear verbatim in logs.
    """

    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是字符串")
    path = value.strip()
    if not path or len(path) > 4096:
        raise ValidationError(f"{field} 必须是有效的绝对路径")
    if any(ord(character) < 32 for character in path):
        raise ValidationError(f"{field} 包含不支持的字符")
    windows_path = PureWindowsPath(path)
    posix_path = PurePosixPath(path)
    if not (windows_path.is_absolute() or posix_path.is_absolute()):
        raise ValidationError(f"{field} 必须是绝对路径")
    if any(part == ".." for part in windows_path.parts) or any(
        part == ".." for part in posix_path.parts
    ):
        raise ValidationError(f"{field} 不允许包含上级目录跳转")
    # Windows device paths can address non-file objects and must never be used as
    # a template source. UNC paths remain supported.
    if path.startswith(("\\\\?\\", "\\\\.\\")):
        raise ValidationError(f"{field} 不支持设备路径")
    return path


def _validate_source_namespace(value: Any, source_number: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"library_sources[{source_number}].namespace 必须是字符串")
    namespace = value.strip()
    if not _NAMESPACE_PATTERN.fullmatch(namespace):
        raise ValidationError(
            f"library_sources[{source_number}].namespace 格式无效"
        )
    if namespace in {"managed", "legacy"}:
        raise ValidationError(
            f"library_sources[{source_number}].namespace 使用了保留名称"
        )
    return namespace


def _validate_source_tags(value: Any, source_number: int) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_SOURCE_TAGS:
        raise ValidationError(
            f"library_sources[{source_number}].tags 必须是至多 {MAX_SOURCE_TAGS} 项的列表"
        )
    tags: list[str] = []
    seen: set[str] = set()
    for tag_number, value_tag in enumerate(value):
        if not isinstance(value_tag, str):
            raise ValidationError(
                f"library_sources[{source_number}].tags[{tag_number}] 必须是字符串"
            )
        tag = " ".join(value_tag.split()).strip()
        if not tag or len(tag) > 100:
            raise ValidationError(
                f"library_sources[{source_number}].tags[{tag_number}] 格式无效"
            )
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    return tags


def parse_library_sources(value: Any) -> list[dict[str, Any]]:
    """Strictly validate and normalise AstrBot ``template_list`` sources.

    The returned value remains schema-compatible (including ``enabled`` and
    ``__template_key``), so it is safe both for startup parsing and Page config
    persistence.  Backend mappings are derived only after this function succeeds.
    """

    if not isinstance(value, list):
        raise ValidationError("library_sources 必须是列表")
    if len(value) > MAX_LIBRARY_SOURCES:
        raise ValidationError(
            f"library_sources 最多允许 {MAX_LIBRARY_SOURCES} 个来源"
        )

    parsed: list[dict[str, Any]] = []
    namespaces: set[str] = set()
    for source_number, raw_source in enumerate(value):
        if not isinstance(raw_source, dict):
            raise ValidationError(f"library_sources[{source_number}] 必须是对象")
        template = raw_source.get("__template_key")
        if not isinstance(template, str) or template not in LIBRARY_SOURCE_TEMPLATES:
            raise ValidationError(
                f"library_sources[{source_number}] 来源类型无效"
            )
        enabled = raw_source.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValidationError(
                f"library_sources[{source_number}].enabled 必须是布尔值"
            )
        namespace = _validate_source_namespace(
            raw_source.get("namespace"), source_number
        )
        namespace_key = namespace.casefold()
        if namespace_key in namespaces:
            raise ValidationError("library_sources 包含重复的 namespace")
        namespaces.add(namespace_key)

        if template == "json":
            allowed_keys = {
                "__template_key",
                "enabled",
                "index_path",
                "data_root",
                "namespace",
            }
            if set(raw_source) - allowed_keys:
                raise ValidationError(
                    f"library_sources[{source_number}] 包含不支持的字段"
                )
            parsed.append(
                {
                    "__template_key": "json",
                    "enabled": enabled,
                    "index_path": _validate_source_path(
                        raw_source.get("index_path"),
                        f"library_sources[{source_number}].index_path",
                    ),
                    "data_root": _validate_source_path(
                        raw_source.get("data_root"),
                        f"library_sources[{source_number}].data_root",
                    ),
                    "namespace": namespace,
                }
            )
            continue

        allowed_keys = {
            "__template_key",
            "enabled",
            "root",
            "namespace",
            "recursive",
            "tags",
        }
        if set(raw_source) - allowed_keys:
            raise ValidationError(
                f"library_sources[{source_number}] 包含不支持的字段"
            )
        recursive = raw_source.get("recursive", True)
        if not isinstance(recursive, bool):
            raise ValidationError(
                f"library_sources[{source_number}].recursive 必须是布尔值"
            )
        parsed.append(
            {
                "__template_key": "directory",
                "enabled": enabled,
                "root": _validate_source_path(
                    raw_source.get("root"),
                    f"library_sources[{source_number}].root",
                ),
                "namespace": namespace,
                "recursive": recursive,
                "tags": _validate_source_tags(
                    raw_source.get("tags", []), source_number
                ),
            }
        )
    return parsed


def _parse_query_int(
    value: Any, field: str, default: int, minimum: int, maximum: int
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValidationError(f"{field} 必须是整数")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal():
        parsed = int(value)
    else:
        raise ValidationError(f"{field} 必须是整数")
    if not minimum <= parsed <= maximum:
        raise ValidationError(f"{field} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def _parse_query_text(value: Any, field: str, maximum: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是字符串")
    parsed = value.strip()
    if len(parsed) > maximum:
        raise ValidationError(f"{field} 过长")
    return parsed


def parse_list_query(values: Mapping[str, Any]) -> ListQuery:
    """Parse and bound list endpoint query parameters."""

    page = _parse_query_int(values.get("page"), "page", 1, 1, 1_000_000)
    page_size = _parse_query_int(values.get("page_size"), "page_size", 50, 1, 100)
    q = _parse_query_text(values.get("q"), "q", 200)
    tag = _parse_query_text(values.get("tag"), "tag", 100)
    sort = values.get("sort", "filename")
    if not isinstance(sort, str) or sort not in LIST_SORTS:
        raise ValidationError("sort 无效")
    return ListQuery(page=page, page_size=page_size, q=q, tag=tag, sort=sort)


def safe_thumbnail_size(value: Any, default: int = 200) -> int:
    """Return a safe configured thumbnail size even for legacy bad config."""

    try:
        return _validate_int(value, "thumbnail_size", 50, 400)
    except ValidationError:
        return default
