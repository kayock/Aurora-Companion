"""Optional embeddings client (semantic recall) with a SQLite-backed cache.

Semantic recall is an enhancement, never a requirement: when no embedding
endpoint is configured, retrieval falls back to SQLite FTS5 + lexical
ranking (the fusion mirrors Shikigami's BM25+vector RRF approach, with
FTS5 standing in for BM25 and stored float vectors for the vector side).
"""

from __future__ import annotations

import hashlib
import math
import struct

import httpx


def _vec_key(base_url: str, model: str, text: str) -> str:
    return hashlib.sha256(f"{base_url}|{model}|{text}".encode()).hexdigest()


class EmbeddingClient:
    def __init__(self, base_url: str, model: str, db=None, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.db = db
        self._client = httpx.AsyncClient(timeout=timeout)
        self.available: bool | None = None  # None = not probed yet

    async def embed(self, texts: list[str]) -> list[list[float] | None]:
        """Embed texts; returns None per text on any failure (graceful
        degradation, as Luna's embedmem does). Cached rows avoid repeat calls."""
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[int] = []
        if self.db is not None:
            for i, text in enumerate(texts):
                row = self.db.query_one(
                    "SELECT dim, data FROM embeddings WHERE key=?", (_vec_key(self.base_url, self.model, text),)
                )
                if row is not None:
                    results[i] = list(struct.unpack(f"{row['dim']}f", row["data"]))
                else:
                    missing.append(i)
        else:
            missing = list(range(len(texts)))
        if not missing:
            self.available = True
            return results
        try:
            resp = await self._client.post(
                f"{self.base_url}/embeddings" if "/embeddings" not in self.base_url else self.base_url,
                json={"model": self.model, "input": [texts[i] for i in missing]},
            )
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
            for pos, i in enumerate(missing):
                vec = data[pos]["embedding"]
                results[i] = vec
                if self.db is not None:
                    self.db.execute(
                        "INSERT OR REPLACE INTO embeddings (key, dim, data) VALUES (?, ?, ?)",
                        (_vec_key(self.base_url, self.model, texts[i]), len(vec), struct.pack(f"{len(vec)}f", *vec)),
                    )
            self.available = True
        except (httpx.HTTPError, KeyError, ValueError):
            self.available = False
        return results

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    async def close(self) -> None:
        await self._client.aclose()
