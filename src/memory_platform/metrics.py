"""Domain metrics — the numbers this platform is actually judged on.

Before this module, `/metrics` served `http_request_duration_seconds` and some
process gauges. Every one of those is true of any FastAPI service, and none of
them can answer a single question from `04-EVALUATION.md`: is p95 context under
350 ms, is the review backlog growing, is extraction still enabled, are we
serving packs degraded because the embedder is cold.

So the metrics here are chosen from the production gate (§7.3) and the capability
scorecard, not from what is easy to instrument:

  p95 memory.context < 350 ms .... memory_context_duration_seconds
  write -> retrievable p99 < 5 s . memory_write_duration_seconds
  inbox median depth < 40 ........ memory_inbox_depth
  isolation 100% ................. memory_scope_denied_total
  acceptance in 30-85% ........... memory_extraction_acceptance_rate
  trust attribution >= 95% ....... memory_pack_items_total{tier}

WHERE EACH HALF LIVES, AND WHY IT HAD TO SPLIT.

Request-time metrics are recorded by the API from work it has already done for a
caller who already proved their scope. There is no isolation question: we are
counting what we served.

Backlog and curation gauges are different — they are aggregates ACROSS projects,
and the API runs as `memory_app`, which is NOBYPASSRLS. A scrape carries no
scope, so the API physically cannot compute them, and giving it a BYPASSRLS
connection to make a dashboard prettier would put a role that can read every
tenant inside the process that serves untrusted callers. That trade is not worth
a graph. The scheduler already opens a scoped transaction per bound project to
sample `mem.curation_metrics`, so the gauges are published from there instead,
over its own listener.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("memory.metrics")

try:
    from prometheus_client import (
        CollectorRegistry, Counter, Gauge, Histogram, start_http_server,
    )
    _AVAILABLE = True
except ImportError:  # pragma: no cover - prometheus_client ships with the API
    _AVAILABLE = False

if _AVAILABLE:
    # ---------------------------------------------------------------- packs
    # Buckets straddle the 350 ms production gate deliberately: default
    # Prometheus buckets jump 0.25 -> 0.5, so a p95 sitting at 340 ms and one at
    # 490 ms land in the same bucket and the gate becomes unmeasurable at
    # exactly the value it is written about.
    CONTEXT_LATENCY = Histogram(
        "memory_context_duration_seconds",
        "End-to-end latency of a context pack build.",
        ["intent", "degraded"],
        buckets=(0.05, 0.1, 0.2, 0.3, 0.35, 0.5, 0.75, 1.0, 2.0, 5.0, 10.0),
    )
    CONTEXT_STAGE = Histogram(
        "memory_context_stage_duration_seconds",
        "Per-stage latency inside a pack build (profile, embed, search, rerank, assemble).",
        ["stage"],
        buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 3.0, 8.0),
    )
    PACK_TOKENS = Histogram(
        "memory_context_pack_tokens",
        "Tokens actually spent on the pack — the cost the agent pays for memory.",
        buckets=(64, 128, 256, 512, 1024, 2048, 4000, 8000, 16000),
    )
    PACK_ITEMS = Counter(
        "memory_pack_items_total",
        "Items returned in packs, by section and trust tier. Trust attribution (C4).",
        ["section", "tier"],
    )
    PACK_DROPPED = Counter(
        "memory_pack_dropped_total",
        "Candidates dropped during assembly, by reason.",
        ["reason"],
    )
    PACK_DEGRADED = Counter(
        "memory_pack_degraded_total",
        "Packs built with a retrieval arm missing — usually a cold embedder (ADR-0008).",
    )
    ARM_HITS = Histogram(
        "memory_retrieval_arm_hits",
        "Rows returned per retrieval arm before fusion. A flat zero means an arm is dead.",
        ["arm"],
        buckets=(0, 1, 2, 5, 10, 20, 50, 100),
    )

    # --------------------------------------------------------------- writes
    WRITE_LATENCY = Histogram(
        "memory_write_duration_seconds",
        "Write path latency, including the synchronous embed.",
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
    )
    WRITES = Counter(
        "memory_writes_total",
        "Memories written, by source, assigned tier and resulting status.",
        ["source_type", "tier", "status"],
    )
    INJECTION_FLAGGED = Counter(
        "memory_injection_flagged_total",
        "Writes the injection heuristic flagged, by the signal that fired (Suite 5).",
        ["signal"],
    )
    SECRETS_REJECTED = Counter(
        "memory_secret_rejected_total",
        "Ingest attempts hard-rejected by the secret scanner.",
    )

    # ------------------------------------------------------------ isolation
    SCOPE_DENIED = Counter(
        "memory_scope_denied_total",
        "Requests refused on scope or auth grounds. Suite 2's target is zero leakage; "
        "this counting UP is the system working, not failing.",
        ["reason"],
    )
    RATE_LIMITED = Counter(
        "memory_rate_limited_total", "Requests rejected by rate limiting.", ["kind"])
    BACKPRESSURE = Counter(
        "memory_backpressure_total", "Writes refused by admission control.")

    # ------------------------------------------------- curation (scheduler)
    INBOX_DEPTH = Gauge(
        "memory_inbox_depth",
        "Items awaiting human review. ADR-0015's failure mode is this number "
        "growing unnoticed.",
        ["project"],
    )
    INBOX_OLDEST = Gauge(
        "memory_inbox_oldest_days",
        "Age of the oldest unreviewed item, in days.", ["project"])
    CONFLICTS_OPEN = Gauge(
        "memory_conflicts_open", "Unresolved conflicts.", ["project"])
    EXTRACTION_ENABLED = Gauge(
        "memory_extraction_enabled",
        "1 when LLM extraction is permitted, 0 when the ADR-0015 kill switch has "
        "fired. A dashboard showing 0 with no alert is how a feature stays off for "
        "a month without anyone noticing.",
        ["project"],
    )
    ACCEPTANCE_RATE = Gauge(
        "memory_extraction_acceptance_rate",
        "Promoted / decided over the trailing window. Healthy band is 0.30-0.85 — "
        "both ends are failures.",
        ["project"],
    )
    MEMORY_COUNT = Gauge(
        "memory_memories", "Memories by status.", ["project", "status"])


def _labels(*values: Any) -> tuple[str, ...]:
    """Labels must be low-cardinality strings; None would create a series per null."""
    return tuple("unknown" if v is None else str(v) for v in values)


# ---------------------------------------------------------------------------
# Recording helpers. Every one is a no-op when prometheus_client is absent and
# never raises: a metrics bug must not become an outage on the serving path.
# ---------------------------------------------------------------------------

def record_pack(pack: dict[str, Any]) -> None:
    """Record one built context pack. Called with the pack the caller receives."""
    if not _AVAILABLE:
        return
    try:
        plan = pack.get("plan") or {}
        degraded = bool(pack.get("degraded"))
        CONTEXT_LATENCY.labels(
            *_labels(plan.get("intent"), str(degraded).lower())
        ).observe((pack.get("latency_ms") or 0) / 1000.0)

        for stage, ms in (pack.get("timings_ms") or {}).items():
            if stage != "total":
                CONTEXT_STAGE.labels(*_labels(stage)).observe((ms or 0) / 1000.0)

        budget = pack.get("budget") or {}
        PACK_TOKENS.observe(budget.get("used") or 0)
        if degraded:
            PACK_DEGRADED.inc()

        for section, items in (pack.get("sections") or {}).items():
            for item in items or []:
                if isinstance(item, dict):
                    PACK_ITEMS.labels(*_labels(section, item.get("trust"))).inc()

        for d in pack.get("dropped") or []:
            # Reasons carry ids and token counts; keep only the stable prefix or
            # every dropped item mints its own time series.
            reason = str((d or {}).get("reason", "unknown")).split("(")[0].strip()
            PACK_DROPPED.labels(*_labels(reason[:40])).inc()
    except Exception as exc:  # noqa: BLE001
        log.debug("pack metrics skipped: %s", exc)


def record_arms(arm_results: dict[str, Any]) -> None:
    if not _AVAILABLE:
        return
    try:
        for arm, n in (arm_results or {}).items():
            ARM_HITS.labels(*_labels(arm)).observe(
                len(n) if isinstance(n, list) else (n or 0))
    except Exception as exc:  # noqa: BLE001
        log.debug("arm metrics skipped: %s", exc)


def record_write(row: dict[str, Any], seconds: float | None = None) -> None:
    if not _AVAILABLE:
        return
    try:
        WRITES.labels(*_labels(row.get("source_type") or row.get("source"),
                               row.get("tier"), row.get("status"))).inc()
        if seconds is not None:
            WRITE_LATENCY.observe(seconds)
        for signal in (row.get("metadata") or {}).get("injection") or []:
            # "line 2: agent-directive ('AI agents: you must')" -> agent-directive
            name = str(signal).split(":", 1)[-1].split("(")[0].strip()
            INJECTION_FLAGGED.labels(*_labels(name[:40])).inc()
    except Exception as exc:  # noqa: BLE001
        log.debug("write metrics skipped: %s", exc)


def denied(reason: str) -> None:
    if _AVAILABLE:
        SCOPE_DENIED.labels(*_labels(reason)).inc()


def rate_limited(kind: str) -> None:
    if _AVAILABLE:
        RATE_LIMITED.labels(*_labels(kind)).inc()


def backpressure() -> None:
    if _AVAILABLE:
        BACKPRESSURE.inc()


def secret_rejected() -> None:
    if _AVAILABLE:
        SECRETS_REJECTED.inc()


# ---------------------------------------------------------------------------
# Scheduler-side gauges
# ---------------------------------------------------------------------------

def publish_curation(project: str, status: dict[str, Any],
                     counts: dict[str, Any] | None = None) -> None:
    """Publish curation gauges for one project, from the scheduler's sample."""
    if not _AVAILABLE:
        return
    try:
        INBOX_DEPTH.labels(*_labels(project)).set(status.get("inbox_depth") or 0)
        INBOX_OLDEST.labels(*_labels(project)).set(status.get("oldest_days") or 0)
        EXTRACTION_ENABLED.labels(*_labels(project)).set(
            1 if status.get("extraction_allowed") else 0)

        rate = (status.get("acceptance") or {}).get("rate")
        if rate is not None:
            ACCEPTANCE_RATE.labels(*_labels(project)).set(rate)

        for status_name, n in (counts or {}).items():
            MEMORY_COUNT.labels(*_labels(project, status_name)).set(n or 0)
    except Exception as exc:  # noqa: BLE001
        log.debug("curation gauges skipped: %s", exc)


def publish_conflicts(project: str, open_count: int) -> None:
    if _AVAILABLE:
        CONFLICTS_OPEN.labels(*_labels(project)).set(open_count)


def serve(port: int) -> bool:
    """Start a metrics listener. Used by the scheduler, which has no HTTP server.

    The API does not call this — `prometheus_fastapi_instrumentator` already
    exposes /metrics there, and a second registry on another port would split the
    same process's metrics across two scrape targets.
    """
    if not _AVAILABLE:
        return False
    try:
        start_http_server(port)
        log.info("scheduler metrics listening on :%d", port)
        return True
    except OSError as exc:
        # Losing metrics must never stop the scheduler from ingesting.
        log.warning("could not start metrics listener on :%d: %s", port, exc)
        return False
