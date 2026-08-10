"""Cross-encoder rerank — the optional stage between RRF and the feature model.

ADR-0013 and 00-MASTER-BLUEPRINT.md §5.3: "Optional cross-encoder rerank of the
top 40 sits between RRF and the feature model, behind a flag, enabled per-project
only if it wins on the eval set."

WHY IT IS THE RIGHT TOOL FOR THE FAILURE WE MEASURED. The golden set shows the
expected memory is always in the candidate pool but frequently ranked in the
bottom half. Bi-encoder retrieval compares a query vector against a document
vector produced independently, so a 4000-character ADR is one averaged point and
the single paragraph that answers the question is averaged away. A cross-encoder
reads the query and the document together and scores the pair, which is exactly
the discrimination the averaged vector lost.

WHY IT IS OFF BY DEFAULT. It is a second model on the hot path. The blueprint
requires it to earn its latency against the eval set before anyone enables it,
and this module exists so that comparison can be run — not so it can be switched
on because it sounds better.

DEGRADATION IS SILENT-SAFE, NOT SILENT. If the reranker is unreachable or slow,
retrieval falls back to the RRF ordering and says so in the returned payload
(`rerank: {"applied": false, "reason": ...}`). The alternative — failing the
query — would make an optional accuracy improvement into a new hard dependency,
which is precisely what ADR-0013's "behind a flag" is guarding against.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from .config import settings

log = logging.getLogger("memory.reranker")


class RerankUnavailable(RuntimeError):
    """Reranker could not be reached. Callers keep the RRF ordering."""


def available() -> bool:
    return bool(settings().rerank_enabled)


# Documents per request. TEI validates the whole batch against
# --max-batch-tokens and rejects the request outright with 413 rather than
# splitting it, so the client has to do the splitting. This bit us for real:
# lengthening the digests pushed a 32-document batch over the limit, the
# cross-encoder started returning 413 on every call, and retrieval silently fell
# back to RRF ordering — the accuracy gain vanished with no error anywhere except
# the degradation reason this module records. Batching removes the coupling
# between digest length and whether reranking happens at all.
#
# 8 STAYS, and it was tested rather than assumed. A batch of 1 measured 2157 ms
# against a batch of 8 at 1986 ms, which looks like fixed per-request overhead —
# implying fewer, larger round trips would be much faster. So the server was
# given --max-batch-tokens 8192 --auto-truncate and this was raised to 32.
#
# It got WORSE: 40 candidates went 7374 ms -> 19422 ms, and a batch of 8 went
# 1986 -> 2947 ms. TEI logs `forcing max_batch_requests=8`, so it processes 8
# pairs at a time whatever the ceiling says; the larger value only padded each
# batch to a longer sequence. The cost is per-token compute, not per-request
# overhead, and the original reading was wrong.
#
# Reduce MEMORY_RERANK_TOP_K to make reranking cheaper. Not this.
BATCH = int(os.environ.get("MEMORY_RERANK_BATCH", "8"))


def rerank_pairs(query: str, texts: list[str], *, timeout: float = 30.0) -> list[float]:
    """Score each text against the query. Returns scores in input order.

    TEI's /rerank returns [{index, score}] sorted by score, so the indices are
    mapped back to input order here — a caller that assumed the response order
    matched its input would silently scramble every result.
    """
    if not texts:
        return []
    from .telemetry import tracer

    url = settings().rerank_url.rstrip("/") + "/rerank"
    scores = [0.0] * len(texts)

    for start in range(0, len(texts), BATCH):
        chunk = texts[start:start + BATCH]
        payload = {"query": query, "texts": chunk, "raw_scores": False}
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        # One span per batch, for the same reason as the embedder: this is a
        # urllib call the httpx instrumentation cannot see. Per batch rather
        # than per call because batching is what fixed the silent 413, and a
        # span count that stops matching ceil(n/BATCH) is how a regression there
        # would show up.
        with tracer("memory.reranker").start_as_current_span("rerank.http") as span:
            try:
                span.set_attribute("rerank.batch_size", len(chunk))
                span.set_attribute("http.url", url)
            except Exception:  # noqa: BLE001
                pass
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.load(r)
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                raise RerankUnavailable(f"{url}: {exc}") from exc

        if not isinstance(data, list):
            raise RerankUnavailable(f"unexpected rerank response: {str(data)[:200]}")

        for item in data:
            try:
                # Indices are relative to the chunk, not the original list.
                scores[start + int(item["index"])] = float(item["score"])
            except (KeyError, ValueError, IndexError, TypeError) as exc:
                raise RerankUnavailable(f"malformed rerank item {item!r}: {exc}") from exc

    return scores


def apply(
    query: str,
    candidates: list[dict[str, Any]],
    *,
    top_k: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rerank the top_k candidates by cross-encoder score.

    Returns (candidates, meta). On any failure the input order is returned
    unchanged, so this can be dropped into a pipeline without a try/except at
    every call site.
    """
    if not candidates:
        return candidates, {"applied": False, "reason": "no candidates"}
    if not available():
        return candidates, {"applied": False, "reason": "disabled (MEMORY_RERANK_ENABLED)"}

    k = top_k or settings().rerank_top_k
    head, tail = candidates[:k], candidates[k:]

    # Score against title + digest rather than full content: the cross-encoder
    # truncates at 512 tokens anyway, and a digest is the part written to be
    # representative. Feeding it the first 512 tokens of a long document would
    # often be the Context section of an ADR rather than its Decision.
    texts = [f"{c.get('title') or ''}\n{c.get('digest') or ''}".strip() for c in head]

    try:
        scores = rerank_pairs(query, texts)
    except RerankUnavailable as exc:
        log.warning("cross-encoder unavailable, keeping RRF order: %s", exc)
        return candidates, {"applied": False, "reason": f"unavailable: {exc}"}

    for c, s in zip(head, scores):
        c["cross_score"] = round(float(s), 6)

    head.sort(key=lambda c: (c.get("cross_score", 0.0), str(c.get("id"))), reverse=True)
    return head + tail, {
        "applied": True, "scored": len(head), "top_k": k,
        "model": "bge-reranker-base",
    }
