import numpy as np


class MemeEmbedder:
    def __init__(self, index, embedding_provider):
        from .index import MemeIndex

        self.index: MemeIndex = index
        self.provider = embedding_provider

    @property
    def ready(self) -> bool:
        return self.provider is not None

    async def rank(
        self,
        candidates: list[dict],
        query_tags: list[str],
    ) -> list[tuple[str, float]]:
        if not self.ready or not candidates:
            return [(c["id"], 0.0) for c in candidates]

        query_text = self._tags_to_text(query_tags)
        candidate_texts = [
            self._tags_to_text(
                self.index.images.get(c["id"], {}).get("tags", [])
            )
            for c in candidates
        ]

        try:
            query_vec = await self.provider.get_embedding(query_text)
            cand_vecs = await self.provider.get_embeddings(candidate_texts)
        except Exception:
            return [(c["id"], 0.0) for c in candidates]

        if not query_vec or not cand_vecs:
            return [(c["id"], 0.0) for c in candidates]

        query_v = np.array(query_vec, dtype=np.float32)
        query_norm = query_v / (np.linalg.norm(query_v) + 1e-12)

        ranked: list[tuple[str, float]] = []
        for i, c in enumerate(candidates):
            if i >= len(cand_vecs):
                ranked.append((c["id"], 0.0))
                continue
            cv = np.array(cand_vecs[i], dtype=np.float32)
            cv_norm = cv / (np.linalg.norm(cv) + 1e-12)
            sim = float(np.dot(cv_norm, query_norm))
            ranked.append((c["id"], round(sim, 4)))

        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked

    def _tags_to_text(self, tags: list[str]) -> str:
        return " ".join(tags)
