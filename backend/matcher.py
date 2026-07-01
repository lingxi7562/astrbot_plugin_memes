from typing import Any


class TagMatcher:
    def __init__(self, index, synonyms: dict[str, str] | None = None):
        from .index import MemeIndex

        self.index: MemeIndex = index
        self.synonyms: dict[str, str] = synonyms or {}
        self.embedder = None

    def set_embedder(self, embedder) -> None:
        self.embedder = embedder

    def match(
        self,
        query_tags: list[str],
        limit: int = 10,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        if not self.index.tag_to_ids:
            self.index._build_inverted_index()

        expanded = self._expand_synonyms(query_tags)
        scores: dict[str, float] = {}

        for qtag in expanded:
            qtag_lower = qtag.lower().strip()
            if not qtag_lower:
                continue

            if qtag_lower in self.index.tag_to_ids:
                for img_id in self.index.tag_to_ids[qtag_lower]:
                    scores[img_id] = scores.get(img_id, 0.0) + 10.0

            for tag, ids in self.index.tag_to_ids.items():
                if qtag_lower == tag:
                    continue
                if qtag_lower in tag:
                    for img_id in ids:
                        scores[img_id] = scores.get(img_id, 0.0) + 3.0
                if tag in qtag_lower:
                    for img_id in ids:
                        scores[img_id] = scores.get(img_id, 0.0) + 1.5

            for img_id, item in self.index.images.items():
                fname = item.get("filename", "").lower()
                if qtag_lower in fname:
                    scores[img_id] = scores.get(img_id, 0.0) + 1.0

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        results: list[dict[str, Any]] = []
        for img_id, score in ranked:
            if limit > 0 and len(results) >= limit:
                break
            if score < min_score:
                continue
            item = self.index.images[img_id]
            item_tags = [t.lower() for t in item.get("tags", [])]
            matched = []
            for qtag in expanded:
                qtag_lower = qtag.lower()
                for it in item_tags:
                    if qtag_lower == it or qtag_lower in it or it in qtag_lower:
                        matched.append(qtag)
                        break
                for ot in query_tags:
                    ot_lower = ot.lower()
                    for it in item_tags:
                        if ot_lower == it or ot_lower in it or it in ot_lower:
                            if ot not in matched:
                                matched.append(ot)
                            break
            results.append(
                {
                    "id": img_id,
                    "filename": item.get("filename", ""),
                    "rel_path": item.get("rel_path", ""),
                    "path": str(self.index.get_abs_path(item)),
                    "tags": item.get("tags", []),
                    "score": round(score, 2),
                    "matched_tags": matched,
                }
            )
        return results

    async def match_embedding(
        self,
        query_tags: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        if self.embedder is None or not self.embedder.ready:
            return []

        coarse_limit = max(limit * 3, 30)
        keyword_results = self.match(query_tags, coarse_limit, min_score=0.0)
        if not keyword_results:
            return []

        ranked = await self.embedder.rank(keyword_results, query_tags)
        results: list[dict[str, Any]] = []
        for img_id, score in ranked:
            if len(results) >= limit:
                break
            if score <= 0.0:
                continue
            item = self.index.images.get(img_id)
            if item is None:
                continue
            results.append(self._enrich_result(img_id, item, score, list(query_tags)))
        return results

    async def match_hybrid(
        self,
        query_tags: list[str],
        limit: int = 10,
        min_score: float = 0.0,
    ) -> list[dict[str, Any]]:
        if self.embedder is None or not self.embedder.ready:
            return self.match(query_tags, limit, min_score)

        coarse_limit = max(limit * 3, 30)
        keyword_results = self.match(query_tags, coarse_limit, min_score)
        if not keyword_results:
            return []

        ranked = await self.embedder.rank(keyword_results, query_tags)
        keyword_by_id = {r["id"]: r for r in keyword_results}
        results: list[dict[str, Any]] = []
        for img_id, score in ranked:
            if len(results) >= limit:
                break
            if score <= 0.0:
                continue
            kw = keyword_by_id.get(img_id)
            if kw is None:
                continue
            item = self.index.images.get(img_id)
            if item is None:
                continue
            results.append(
                self._enrich_result(img_id, item, score, kw.get("matched_tags", []))
            )
        return results

    def _enrich_result(
        self, img_id: str, item: dict[str, Any], score: float, matched_tags: list[str]
    ) -> dict[str, Any]:
        return {
            "id": img_id,
            "filename": item.get("filename", ""),
            "rel_path": item.get("rel_path", ""),
            "path": str(self.index.get_abs_path(item)),
            "tags": item.get("tags", []),
            "score": round(score, 2),
            "matched_tags": matched_tags,
        }

    def _expand_synonyms(self, tags: list[str]) -> list[str]:
        result = list(tags)
        for tag in tags:
            for src, dst in self.synonyms.items():
                if tag == src and dst not in result:
                    result.append(dst)
                if tag == dst and src not in result:
                    result.append(src)
        return result
