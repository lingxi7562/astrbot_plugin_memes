"""Public, matcher-compatible interface for the multi-source meme library."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

from .library import (
    DirectorySource,
    JsonSource,
    LibraryLoadError,
    SourceConfigurationError,
    SourceStatus,
    build_snapshot,
    json_source_from_value,
    make_report,
)


SourceValue = JsonSource | DirectorySource | str | Path | Mapping[str, Any]


class MemeIndex:
    """Atomically loaded collection of memes from JSON and directory sources.

    ``index_path`` and ``data_root`` retain the original two-argument API.  New
    callers may additionally pass either a unified ``sources`` list (each mapping
    has ``type: json|directory``) or the explicit ``json_sources`` and
    ``directory_sources`` lists.
    """

    def __init__(
        self,
        index_path: str | Path = "",
        data_root: str | Path = "",
        *,
        sources: Sequence[SourceValue] | None = None,
        json_sources: Sequence[JsonSource | str | Path | Mapping[str, Any]] | None = None,
        directory_sources: Sequence[
            DirectorySource | str | Path | Mapping[str, Any]
        ]
        | None = None,
        allowed_roots: Sequence[str | Path] | None = None,
    ):
        self.index_path = Path(index_path) if str(index_path).strip() else Path()
        self.data_root = Path(data_root) if str(data_root).strip() else Path()
        self.images: dict[str, dict[str, Any]] = {}
        self.tag_to_ids: dict[str, list[str]] = {}
        self._all_tags: list[str] = []
        self._resolved_paths: dict[str, Path] = {}
        self._item_roots: dict[str, Path] = {}
        self._source_roots: frozenset[Path] = frozenset()
        self._json_sources: list[JsonSource | str | Path | Mapping[str, Any]] = []
        self._directory_sources: list[
            DirectorySource | str | Path | Mapping[str, Any]
        ] = []
        self._configuration_errors: list[SourceStatus] = []
        # Validation is deliberately deferred to load(), where errors can be
        # reported uniformly and cannot partially mutate a live index.
        self.allowed_roots = tuple(allowed_roots or ())
        self.source_statuses: list[dict[str, Any]] = []
        self.last_report: dict[str, Any] = {
            "status": "not_loaded",
            "committed": False,
            "count": 0,
            "source_count": 0,
            "missing_file_count": 0,
            "duplicate_count": 0,
            "error_count": 0,
            "sources": [],
        }

        # The legacy pair is one ordinary JSON source, with a stable explicit
        # namespace.  No source is created for two empty legacy values.
        if str(index_path).strip():
            legacy_root = self.data_root if str(data_root).strip() else self.index_path.parent
            self._json_sources.append(
                json_source_from_value(
                    self.index_path,
                    default_root=legacy_root,
                    default_namespace="legacy",
                    legacy_ids=True,
                )
            )
        for source in json_sources or ():
            self._json_sources.append(source)
        for source in directory_sources or ():
            self._directory_sources.append(source)
        for source in sources or ():
            self._append_unified_source(source)

    @property
    def json_sources(self) -> tuple[JsonSource | str | Path | Mapping[str, Any], ...]:
        return tuple(self._json_sources)

    @property
    def directory_sources(
        self,
    ) -> tuple[DirectorySource | str | Path | Mapping[str, Any], ...]:
        return tuple(self._directory_sources)

    def add_json_source(
        self, source: JsonSource | str | Path | Mapping[str, Any]
    ) -> None:
        """Add a source definition.  It takes effect on the next ``load``."""

        self._json_sources.append(source)

    def add_directory_source(
        self, source: DirectorySource | str | Path | Mapping[str, Any]
    ) -> None:
        """Add a controlled directory source for the next ``load``."""

        self._directory_sources.append(source)

    def load(self) -> dict[str, Any]:
        """Build all sources and atomically replace the live matcher state.

        Diagnostic state describes the latest attempt even after a failure, while
        ``images``, paths, and inverted indexes remain exactly as last committed.
        """

        if self._configuration_errors:
            report = make_report(self._configuration_errors, 0, committed=False)
            self.last_report = deepcopy(report)
            self.source_statuses = deepcopy(report["sources"])
            raise LibraryLoadError(report)

        try:
            snapshot = build_snapshot(
                self._json_sources,
                self._directory_sources,
                allowed_roots=self.allowed_roots,
            )
        except LibraryLoadError as exc:
            self.last_report = deepcopy(exc.report)
            self.source_statuses = deepcopy(exc.report.get("sources", []))
            raise

        tag_to_ids, all_tags = self._make_inverted_index(snapshot.images)
        item_roots: dict[str, Path] = {}
        for image_id, item in snapshot.images.items():
            # library.py already stores a canonical, resolved source anchor.
            # Preserve that literal anchor instead of resolving it again later:
            # a source directory could otherwise be replaced by a symlink or
            # junction after load and move both the root and child outside it.
            source_root = Path(item["_source_root"])
            if source_root not in snapshot.source_roots:
                raise SourceConfigurationError(
                    "image source root is not part of the validated snapshot"
                )
            item_roots[image_id] = source_root
            # Operational path roots stay private after the candidate snapshot
            # has been validated.
            item.pop("_source_root", None)
        # Commit only after every source and all derived state have succeeded.
        self.images = snapshot.images
        self.tag_to_ids = tag_to_ids
        self._all_tags = all_tags
        self._resolved_paths = snapshot.resolved_paths
        self._item_roots = item_roots
        self._source_roots = snapshot.source_roots
        self.last_report = make_report(snapshot.statuses, len(snapshot.images), committed=True)
        self.source_statuses = deepcopy(self.last_report["sources"])
        return self.get_status_report()

    def _append_unified_source(self, source: SourceValue) -> None:
        if isinstance(source, JsonSource):
            self._json_sources.append(source)
            return
        if isinstance(source, DirectorySource):
            self._directory_sources.append(source)
            return
        if not isinstance(source, Mapping):
            self._record_configuration_error(
                "unified",
                "<invalid>",
                "unified sources must be mappings or JsonSource/DirectorySource objects",
            )
            return
        kind = source.get("type", source.get("kind"))
        if not isinstance(kind, str):
            self._record_configuration_error(
                "unified", str(source.get("path", source.get("root", "<invalid>"))),
                "unified source requires type: json|directory",
            )
            return
        kind = kind.strip().lower()
        if kind in {"json", "index"}:
            self._json_sources.append(source)
        elif kind in {"directory", "dir", "folder"}:
            self._directory_sources.append(source)
        else:
            self._record_configuration_error(
                kind,
                str(source.get("path", source.get("root", "<invalid>"))),
                f"unsupported source type: {kind!r}",
            )

    def _record_configuration_error(self, kind: str, location: str, error: str) -> None:
        status = SourceStatus("configuration", kind, location)
        status.errors.append(error)
        self._configuration_errors.append(status)

    def _build_inverted_index(self) -> None:
        """Rebuild matcher indexes for compatibility with the original class."""

        self.tag_to_ids, self._all_tags = self._make_inverted_index(self.images)

    @staticmethod
    def _make_inverted_index(
        images: Mapping[str, Mapping[str, Any]],
    ) -> tuple[dict[str, list[str]], list[str]]:
        tag_to_ids: dict[str, list[str]] = {}
        all_tags: set[str] = set()
        for image_id, item in images.items():
            tags = item.get("tags", [])
            if not isinstance(tags, (list, tuple)):
                continue
            seen_for_image: set[str] = set()
            for tag in tags:
                if not isinstance(tag, str):
                    continue
                key = tag.strip().casefold()
                if not key or key in seen_for_image:
                    continue
                seen_for_image.add(key)
                all_tags.add(key)
                tag_to_ids.setdefault(key, []).append(image_id)
        return tag_to_ids, sorted(all_tags)

    def get_abs_path(self, item: Mapping[str, Any]) -> Path:
        """Resolve only a live image ID using private canonical path records.

        Caller-supplied ``rel_path`` and root-like fields are never consulted.
        """

        try:
            image_id = item.get("id")
        except AttributeError as exc:
            raise SourceConfigurationError("item must be a mapping from the live index") from exc
        if (
            not isinstance(image_id, str)
            or image_id not in self.images
            or image_id not in self._resolved_paths
            or image_id not in self._item_roots
        ):
            raise SourceConfigurationError("item does not identify a live canonical image")

        root_anchor = self._item_roots[image_id]
        if root_anchor not in self._source_roots:
            raise SourceConfigurationError("image source root is not a live anchor")
        try:
            current_root = root_anchor.resolve(strict=False)
            cached = self._resolved_paths[image_id].resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise SourceConfigurationError("unable to resolve live image path") from exc
        if current_root != root_anchor:
            raise SourceConfigurationError("image source root changed after index load")
        if not self._is_within(cached, root_anchor):
            raise SourceConfigurationError("cached image path escapes its source root")
        return cached

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def get_unique_tags(self) -> list[str]:
        return list(self._all_tags)

    def get_status_report(self) -> dict[str, Any]:
        """Return a defensive copy of the latest load diagnostics."""

        return deepcopy(self.last_report)

    @property
    def count(self) -> int:
        return len(self.images)


__all__ = [
    "DirectorySource",
    "JsonSource",
    "LibraryLoadError",
    "MemeIndex",
    "SourceConfigurationError",
]
