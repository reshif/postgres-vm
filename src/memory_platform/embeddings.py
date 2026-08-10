"""Embedding provider abstraction.

One interface, two adapters. Switching between host Ollama and in-compose TEI is
a settings change, not a code change — which matters because this deployment has
already had to move once (TEI could not fit the hardware; see the note on the
profile-gated `embeddings` service in docker-compose.yml).

Two invariants the rest of the system relies on:

  * DIMENSIONS ARE CHECKED, NOT TRUSTED. mem.memory_embeddings.embedding is
    halfvec(1024) and the HNSW index is built for it. A provider silently
    returning 768 dims (nomic-embed-text, say) would fail at the INSERT with a
    type error far from the cause, so we check on the way out of the provider.

  * VECTORS ARE NORMALISED. The index uses halfvec_cosine_ops, and normalising
    makes cosine and inner product agree. bge-m3 already returns unit vectors
    through both providers, so this is a no-op in the normal case — it exists so
    that swapping in a provider that does NOT normalise cannot quietly change
    what the distances mean.

MEMORY_EMBEDDING_MODEL is the logical registry id (`bge-m3@1`), matching
mem.embedding_models.id. The `@N` suffix versions the vector space so a re-embed
is a new row rather than a silent overwrite of vectors produced by a different
model. Providers strip it to get their own model name.
"""
from __future__ import annotations

import json
import logging
import math
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Protocol

from sqlalchemy import text

from .config import settings

log = logging.getLogger("memory.embeddings")


class EmbeddingUnavailable(RuntimeError):
    """The embedder could not be reached or failed.

    Callers are expected to degrade rather than fail closed (ADR-0008): a memory
    still gets written and is still retrievable through the lexical arm; only the
    vector arm is missing until a backfill runs.
    """


class EmbeddingProvider(Protocol):
    model_id: str
    wire_model: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def _post(url: str, payload: dict, timeout: float) -> object:
    # Traced explicitly. This call goes out over urllib, not httpx, so the httpx
    # auto-instrumentation does not see it — and it is typically the single
    # largest span in a context pack. A trace of a 780 ms pack that accounts for
    # 30 ms of SQL and nothing else sends you looking at the database, which is
    # the one place the time is not going.
    from .telemetry import tracer

    with tracer("memory.embeddings").start_as_current_span("embed.http") as span:
        try:
            span.set_attribute("http.url", url)
            span.set_attribute("embed.batch_size", len(payload.get("input", []) or []))
        except Exception:  # noqa: BLE001 - a no-op span has no set_attribute
            pass
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise EmbeddingUnavailable(f"{url}: {exc}") from exc


def _finalise(vectors: list[list[float]], expected: int, source: str) -> list[list[float]]:
    out = []
    for v in vectors:
        if len(v) != expected:
            raise EmbeddingUnavailable(
                f"{source} returned {len(v)} dimensions, expected {expected}. "
                "The vector column and HNSW index are built for the configured "
                "size — changing embedding model requires a migration, not just "
                "a settings change."
            )
        norm = math.sqrt(sum(x * x for x in v))
        out.append([x / norm for x in v] if norm and abs(norm - 1.0) > 1e-6 else list(v))
    return out


class OllamaProvider:
    """Host-side Ollama. POST /api/embed, batch via the `input` array."""

    def __init__(self, url: str, model_id: str, dimensions: int) -> None:
        self.url = url.rstrip("/")
        self.model_id = model_id
        self.wire_model = model_id.split("@")[0]
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        d = _post(f"{self.url}/api/embed",
                  {"model": self.wire_model, "input": texts}, timeout=120.0)
        vecs = d.get("embeddings") if isinstance(d, dict) else None
        if not vecs:
            raise EmbeddingUnavailable(f"ollama returned no embeddings: {str(d)[:200]}")
        return _finalise(vecs, self.dimensions, "ollama")


class TEIProvider:
    """Text Embeddings Inference. POST /embed, returns a bare array of arrays."""

    def __init__(self, url: str, model_id: str, dimensions: int) -> None:
        self.url = url.rstrip("/")
        self.model_id = model_id
        self.wire_model = model_id.split("@")[0]
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        d = _post(f"{self.url}/embed", {"inputs": texts}, timeout=120.0)
        if not isinstance(d, list) or not d:
            raise EmbeddingUnavailable(f"TEI returned no embeddings: {str(d)[:200]}")
        return _finalise(d, self.dimensions, "TEI")


@lru_cache
def provider() -> EmbeddingProvider:
    s = settings()
    kind = s.embedding_provider.lower()
    if kind == "ollama":
        return OllamaProvider(s.embedding_url, s.embedding_model, s.embedding_dim)
    if kind in ("local", "tei"):
        return TEIProvider(s.embedding_url, s.embedding_model, s.embedding_dim)
    raise ValueError(
        f"unknown MEMORY_EMBEDDING_PROVIDER {s.embedding_provider!r} "
        "(expected 'ollama' or 'local')"
    )


def embed_one(txt: str) -> list[float]:
    return provider().embed([txt])[0]


def to_pgvector(vec: list[float]) -> str:
    """pgvector/halfvec literal form. Cast at the call site: CAST(:v AS halfvec(N))."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def ensure_registered(conn) -> str:
    """Upsert the active model into mem.embedding_models and return its id.

    memory_embeddings.model_id is a foreign key, so a write fails without this
    row. Registering from code rather than a migration keeps the registry honest
    when the provider is swapped by settings alone.
    """
    p = provider()
    conn.execute(
        text(
            "INSERT INTO mem.embedding_models "
            "  (id, provider, dimensions, normalized, is_active, is_primary) "
            "VALUES (:id, :prov, :dim, true, true, true) "
            "ON CONFLICT (id) DO UPDATE SET provider = EXCLUDED.provider, "
            "  dimensions = EXCLUDED.dimensions, is_active = true"
        ),
        {"id": p.model_id, "prov": settings().embedding_provider, "dim": p.dimensions},
    )
    return p.model_id
