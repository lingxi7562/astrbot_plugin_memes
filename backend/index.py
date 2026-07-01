import json
from pathlib import Path
from typing import Any


class MemeIndex:
    def __init__(self, index_path: str, data_root: str):
        self.index_path = Path(index_path)
        self.data_root = Path(data_root)
        self.images: dict[str, dict[str, Any]] = {}
        self.tag_to_ids: dict[str, list[str]] = {}
        self._all_tags: list[str] = []

    def load(self) -> None:
        with open(self.index_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.images = data.get("images", {})
        self._build_inverted_index()

    def _build_inverted_index(self) -> None:
        self.tag_to_ids = {}
        all_tags: list[str] = []
        for img_id, item in self.images.items():
            tags = item.get("tags", [])
            all_tags.extend(tags)
            for tag in tags:
                tag_lower = tag.lower().strip()
                if not tag_lower:
                    continue
                self.tag_to_ids.setdefault(tag_lower, []).append(img_id)
        self._all_tags = sorted(set(t.lower() for t in all_tags))

    def get_abs_path(self, item: dict[str, Any]) -> Path:
        return self.data_root / item["rel_path"]

    def get_unique_tags(self) -> list[str]:
        return self._all_tags

    @property
    def count(self) -> int:
        return len(self.images)
