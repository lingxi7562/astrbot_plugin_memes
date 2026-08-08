"""Provider-backed semantic indexing with a validated persistent cache."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np


class EmbeddingUnavailable(RuntimeError):
    """Raised when a provider cannot produce a complete semantic index."""


class MemeEmbedder:
    """Embed every indexed meme and answer full-library cosine queries.

    The cache contains only normalized vectors and fingerprints.  Provider
    identity, vector dimension, and every item fingerprint must match before a
    vector is reused.  Cache writes are atomic and temporary files are removed
    on every exit path.
    """

    CACHE_SCHEMA_VERSION = 1
    MAX_CACHE_BYTES = 64 * 1024 * 1024
    MAX_CACHE_ITEMS = 100_000
    EMBEDDING_BATCH_SIZE = 64

    def __init__(self, index, embedding_provider, cache_path: str | Path | None = None):
        from .index import MemeIndex

        self.index: MemeIndex = index
        self.provider = embedding_provider
        self.cache_path = Path(cache_path) if cache_path else None
        self._vectors: dict[str, np.ndarray] | None = None
        self._fingerprints: dict[str, str] = {}
        self._index_signature: tuple[tuple[str, str], ...] | None = None
        self._dimension: int | None = None
        self._provider_key = self._provider_fingerprint()
        self._build_lock = asyncio.Lock()

    @property
    def ready(self) -> bool:
        return self.provider is not None

    @property
    def indexed_count(self) -> int:
        return len(self._vectors or {})

    @property
    def cache_status(self) -> dict[str, Any]:
        return {
            "path": str(self.cache_path) if self.cache_path else "",
            "ready": self._vectors is not None,
            "indexed_count": self.indexed_count,
            "library_count": len(self.index.images),
            "dimension": self._dimension,
            "provider_key": self._provider_key[:12],
        }

    async def ensure_index(self) -> None:
        """Build or incrementally restore a complete vector index."""

        if not self.ready:
            raise EmbeddingUnavailable("embedding provider is unavailable")
        signature = self._current_signature()
        if self._vectors is not None and signature == self._index_signature:
            return

        async with self._build_lock:
            for _ in range(3):
                signature = self._current_signature()
                if self._vectors is not None and signature == self._index_signature:
                    return

                dimension = self._provider_dimension()
                vectors = self._read_cache(signature, dimension)
                missing_ids = [
                    image_id for image_id, _ in signature if image_id not in vectors
                ]
                if missing_ids:
                    texts = [self._item_text(self.index.images[image_id]) for image_id in missing_ids]
                    embedded = await self._embed_texts(texts, dimension)
                    vectors.update(zip(missing_ids, embedded, strict=True))
                    if dimension is None and embedded:
                        dimension = int(embedded[0].shape[0])

                # A refresh can occur while the provider call is awaiting.  Do
                # not publish vectors for a mixed generation.
                if signature != self._current_signature():
                    continue
                if len(vectors) != len(signature):
                    raise EmbeddingUnavailable("provider returned an incomplete vector index")

                self._vectors = vectors
                self._fingerprints = dict(signature)
                self._index_signature = signature
                self._dimension = dimension
                self._write_cache(signature, vectors, dimension)
                return
            raise EmbeddingUnavailable("library changed repeatedly during embedding")

    async def rank_all(
        self,
        query_tags: list[str],
        limit: int = 10,
        min_score: float = -1.0,
    ) -> list[tuple[str, float]]:
        await self.ensure_index()
        query = await self._embed_query(self._tags_to_text(query_tags))
        vectors = self._vectors or {}
        ranked = [
            (image_id, round(float(np.dot(vector, query)), 6))
            for image_id, vector in vectors.items()
            if image_id in self.index.images
        ]
        ranked = [row for row in ranked if row[1] >= min_score]
        ranked.sort(key=lambda row: (-row[1], row[0]))
        return ranked[:limit] if limit > 0 else ranked

    async def rank(
        self,
        candidates: list[dict],
        query_tags: list[str],
    ) -> list[tuple[str, float]]:
        """Rank a supplied subset using the same complete semantic index."""

        await self.ensure_index()
        query = await self._embed_query(self._tags_to_text(query_tags))
        candidate_ids = {candidate.get("id") for candidate in candidates}
        ranked = [
            (image_id, round(float(np.dot(vector, query)), 6))
            for image_id, vector in (self._vectors or {}).items()
            if image_id in candidate_ids
        ]
        ranked.sort(key=lambda row: (-row[1], row[0]))
        return ranked

    async def _embed_query(self, text: str) -> np.ndarray:
        try:
            if hasattr(self.provider, "get_embedding"):
                raw = await self._maybe_await(self.provider.get_embedding(text))
            else:
                raw_batch = await self._maybe_await(self.provider.get_embeddings([text]))
                raw = raw_batch[0]
            vectors = self._normalise_vectors([raw], self._dimension)
            if not vectors:
                raise EmbeddingUnavailable("provider returned an empty query vector")
            vector = vectors[0]
            if self._dimension is None:
                self._dimension = int(vector.shape[0])
            return vector
        except EmbeddingUnavailable:
            raise
        except Exception as exc:
            raise EmbeddingUnavailable(f"embedding query failed: {exc}") from exc

    async def _embed_texts(
        self, texts: list[str], dimension: int | None
    ) -> list[np.ndarray]:
        if not texts:
            return []
        try:
            if hasattr(self.provider, "get_embeddings"):
                raw_vectors: list[Any] = []
                for start in range(0, len(texts), self.EMBEDDING_BATCH_SIZE):
                    batch = texts[start : start + self.EMBEDDING_BATCH_SIZE]
                    result = await self._maybe_await(self.provider.get_embeddings(batch))
                    raw_vectors.extend(self._as_vector_rows(result, len(batch)))
            elif hasattr(self.provider, "get_embedding"):
                raw_vectors = await asyncio.gather(
                    *(self._maybe_await(self.provider.get_embedding(text)) for text in texts)
                )
            else:
                raise EmbeddingUnavailable("provider exposes no embedding method")
            vectors = self._normalise_vectors(raw_vectors, dimension)
            if len(vectors) != len(texts):
                raise EmbeddingUnavailable("provider returned the wrong vector count")
            return vectors
        except EmbeddingUnavailable:
            raise
        except Exception as exc:
            raise EmbeddingUnavailable(f"embedding index build failed: {exc}") from exc

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    @staticmethod
    def _as_vector_rows(value: Any, expected_count: int) -> list[Any]:
        array = np.asarray(value, dtype=np.float32)
        if array.ndim == 1 and expected_count == 1:
            return [array]
        if array.ndim != 2 or array.shape[0] != expected_count:
            raise EmbeddingUnavailable("provider returned malformed embedding batches")
        return [row for row in array]

    @staticmethod
    def _normalise_vectors(
        values: Iterable[Any], dimension: int | None
    ) -> list[np.ndarray]:
        vectors: list[np.ndarray] = []
        for raw in values:
            vector = np.asarray(raw, dtype=np.float32)
            if vector.ndim != 1 or vector.size == 0:
                raise EmbeddingUnavailable("embedding vector must be one-dimensional")
            if dimension is not None and vector.size != dimension:
                raise EmbeddingUnavailable("embedding vector dimension changed")
            if not np.isfinite(vector).all():
                raise EmbeddingUnavailable("embedding vector contains non-finite values")
            norm = float(np.linalg.norm(vector))
            if not math.isfinite(norm) or norm <= 1e-12:
                raise EmbeddingUnavailable("embedding vector has zero magnitude")
            vectors.append(vector / norm)
        return vectors

    def _provider_dimension(self) -> int | None:
        try:
            value = self.provider.get_dim()
        except Exception:
            return None
        if isinstance(value, bool):
            return None
        try:
            dimension = int(value)
        except (TypeError, ValueError):
            return None
        return dimension if 1 <= dimension <= 8192 else None

    def _provider_fingerprint(self) -> str:
        safe: dict[str, str] = {
            "class": f"{type(self.provider).__module__}.{type(self.provider).__qualname__}"
        }
        try:
            meta = self.provider.meta()
            for key in ("id", "type", "name", "model"):
                value = getattr(meta, key, None)
                if isinstance(value, (str, int, float, bool)):
                    safe[key] = str(value)
        except Exception:
            pass
        payload = json.dumps(safe, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _current_signature(self) -> tuple[tuple[str, str], ...]:
        values: list[tuple[str, str]] = []
        for image_id, item in self.index.images.items():
            if not isinstance(image_id, str) or not isinstance(item, dict):
                continue
            text = self._item_text(item)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            values.append((image_id, digest))
        values.sort()
        return tuple(values)

    @classmethod
    def _item_text(cls, item: dict[str, Any]) -> str:
        tags = item.get("tags", [])
        tag_text = " ".join(tag for tag in tags if isinstance(tag, str))
        fields = (
            item.get("filename", ""),
            item.get("source", ""),
            item.get("rel_path", ""),
            tag_text,
        )
        return " | ".join(str(field).strip() for field in fields if field is not None)

    @staticmethod
    def _tags_to_text(tags: list[str]) -> str:
        return " ".join(tag.strip() for tag in tags if isinstance(tag, str) and tag.strip())

    def _read_cache(
        self,
        signature: tuple[tuple[str, str], ...],
        dimension: int | None,
    ) -> dict[str, np.ndarray]:
        if self.cache_path is None:
            return {}
        try:
            if not self.cache_path.is_file() or self.cache_path.stat().st_size > self.MAX_CACHE_BYTES:
                return {}
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            if payload.get("schema_version") != self.CACHE_SCHEMA_VERSION:
                return {}
            if payload.get("provider_key") != self._provider_key:
                return {}
            cached_dimension = payload.get("dimension")
            if not isinstance(cached_dimension, int) or not 1 <= cached_dimension <= 8192:
                return {}
            if dimension is not None and cached_dimension != dimension:
                return {}
            raw_items = payload.get("items")
            if not isinstance(raw_items, dict) or len(raw_items) > self.MAX_CACHE_ITEMS:
                return {}
            expected = dict(signature)
            vectors: dict[str, np.ndarray] = {}
            for image_id, record in raw_items.items():
                if image_id not in expected or not isinstance(record, dict):
                    continue
                if record.get("fingerprint") != expected[image_id]:
                    continue
                try:
                    vector = self._normalise_vectors(
                        [record.get("vector")], cached_dimension
                    )
                except EmbeddingUnavailable:
                    continue
                if vector:
                    vectors[image_id] = vector[0]
            return vectors
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}

    def _write_cache(
        self,
        signature: tuple[tuple[str, str], ...],
        vectors: dict[str, np.ndarray],
        dimension: int | None,
    ) -> None:
        if self.cache_path is None or not vectors or dimension is None:
            return
        if len(vectors) > self.MAX_CACHE_ITEMS:
            return
        payload = {
            "schema_version": self.CACHE_SCHEMA_VERSION,
            "provider_key": self._provider_key,
            "dimension": dimension,
            "items": {
                image_id: {
                    "fingerprint": fingerprint,
                    "vector": [float(value) for value in vectors[image_id]],
                }
                for image_id, fingerprint in signature
                if image_id in vectors
            },
        }
        temporary: Path | None = None
        try:
            encoded = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded) > self.MAX_CACHE_BYTES:
                return
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.cache_path.name}.",
                suffix=".tmp",
                dir=self.cache_path.parent,
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            temporary.write_bytes(encoded)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, self.cache_path)
            temporary = None
        except (OSError, TypeError, ValueError):
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


__all__ = ["EmbeddingUnavailable", "MemeEmbedder"]
