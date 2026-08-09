from typing import Any, Iterable


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
        primary_tags: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.index.tag_to_ids:
            self.index._build_inverted_index()

        expanded = self._expand_synonyms(query_tags)
        primary = {
            value.casefold().strip()
            for value in (primary_tags or ())
            if isinstance(value, str) and value.strip()
        }
        scores: dict[str, float] = {}

        for qtag in expanded:
            qtag_lower = qtag.lower().strip()
            if not qtag_lower:
                continue
            weight = 1.8 if qtag_lower in primary else 1.0

            if qtag_lower in self.index.tag_to_ids:
                for img_id in self.index.tag_to_ids[qtag_lower]:
                    scores[img_id] = scores.get(img_id, 0.0) + 10.0 * weight

            for tag, ids in self.index.tag_to_ids.items():
                if qtag_lower == tag:
                    continue
                if qtag_lower in tag:
                    for img_id in ids:
                        scores[img_id] = scores.get(img_id, 0.0) + 3.0 * weight
                if tag in qtag_lower:
                    for img_id in ids:
                        scores[img_id] = scores.get(img_id, 0.0) + 1.5 * weight

            for img_id, item in self.index.images.items():
                fname = item.get("filename", "").lower()
                if qtag_lower in fname:
                    scores[img_id] = scores.get(img_id, 0.0) + 1.0 * weight

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
                    "source": item.get("source", ""),
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
        min_score: float = -1.0,
        primary_tags: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.embedder is None or not self.embedder.ready:
            return []
        try:
            ranked = await self.embedder.rank_all(
                self._prioritize(query_tags, primary_tags),
                limit=limit,
                min_score=min_score,
            )
        except Exception:
            return []
        results: list[dict[str, Any]] = []
        for img_id, score in ranked:
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
        primary_tags: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.embedder is None or not self.embedder.ready:
            return self.match(query_tags, limit, min_score, primary_tags)
        try:
            semantic_rank = await self.embedder.rank_all(
                self._prioritize(query_tags, primary_tags),
                limit=0,
                min_score=-1.0,
            )
        except Exception:
            return []

        # Keyword evidence is optional in hybrid mode: semantic-only matches
        # must remain visible when no tag shares a lexical token with the query.
        keyword_results = self.match(
            query_tags,
            limit=0,
            min_score=0.0,
            primary_tags=primary_tags,
        )
        keyword_by_id = {result["id"]: result for result in keyword_results}
        max_keyword = max(
            (float(result.get("score", 0.0)) for result in keyword_results),
            default=0.0,
        )
        semantic_by_id = dict(semantic_rank)
        all_ids = set(semantic_by_id) | set(keyword_by_id)
        ranked: list[tuple[str, float]] = []
        for img_id in all_ids:
            semantic_score = semantic_by_id.get(img_id, -1.0)
            semantic_normalised = max(0.0, min(1.0, (semantic_score + 1.0) / 2.0))
            keyword_score = float(keyword_by_id.get(img_id, {}).get("score", 0.0))
            keyword_normalised = keyword_score / max_keyword if max_keyword > 0 else 0.0
            combined = 0.65 * semantic_normalised + 0.35 * keyword_normalised
            ranked.append((img_id, combined))
        ranked.sort(key=lambda row: (-row[1], row[0]))
        results: list[dict[str, Any]] = []
        for img_id, score in ranked:
            if len(results) >= limit:
                break
            if score < min_score:
                continue
            item = self.index.images.get(img_id)
            if item is None:
                continue
            keyword_match = keyword_by_id.get(img_id, {})
            results.append(self._enrich_result(
                img_id,
                item,
                score,
                keyword_match.get("matched_tags", []),
            ))
        return results

    @staticmethod
    def _prioritize(
        query_tags: list[str], primary_tags: Iterable[str] | None
    ) -> list[str]:
        primary = [
            value.strip()
            for value in (primary_tags or ())
            if isinstance(value, str) and value.strip()
        ]
        primary_keys = {value.casefold() for value in primary}
        remainder = [
            value
            for value in query_tags
            if isinstance(value, str) and value.strip().casefold() not in primary_keys
        ]
        return primary + remainder

    def _enrich_result(
        self, img_id: str, item: dict[str, Any], score: float, matched_tags: list[str]
    ) -> dict[str, Any]:
        try:
            path = str(self.index.get_abs_path(item))
        except Exception:
            # A source can disappear or be replaced between index refreshes;
            # never turn a single stale item into a failed whole-library query.
            path = ""
        return {
            "id": img_id,
            "filename": item.get("filename", ""),
            "rel_path": item.get("rel_path", ""),
            "source": item.get("source", ""),
            "path": path,
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
