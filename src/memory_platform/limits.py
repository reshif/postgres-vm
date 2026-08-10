"""Admission control — quotas, rate limits and backpressure (Phase 9).

Three different failure modes, three different answers:

  * RATE LIMIT — one tenant sending too many requests per second. Answer: 429
    with Retry-After. The tenant is fine, it is just going too fast.
  * QUOTA — one tenant storing more than its share. Answer: 429 on writes only;
    reads keep working, because cutting off retrieval to punish a write quota
    breaks the thing the tenant is actually paying for.
  * BACKPRESSURE — the SYSTEM is behind: the job queue is deep, or the embedder
    is timing out. Answer: 503 with Retry-After, because this is not the
    caller's fault and a 429 tells them to slow down when the correct action is
    to try again later.

Conflating these is the usual mistake and it produces exactly the wrong
behaviour: a client that backs off on a 503 it should have retried, or a client
that hammers a 429 that will never clear.

IN-PROCESS COUNTERS, DELIBERATELY. This is a fixed-window counter in memory, so
with N API replicas the effective limit is N x the configured value. That is
stated rather than hidden: a shared Redis counter would be exact and would also
add the external dependency this platform spent an ADR avoiding (ADR-0001). At
the point where exactness matters, the counter moves into Postgres alongside
everything else — the interface here does not change.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .config import settings

log = logging.getLogger("memory.limits")


class RateLimited(Exception):
    """Too many requests from this tenant. Caller should slow down (429)."""

    def __init__(self, retry_after: int, detail: str) -> None:
        self.retry_after = retry_after
        super().__init__(detail)


class Overloaded(Exception):
    """The system is behind. Caller should retry later (503)."""

    def __init__(self, retry_after: int, detail: str) -> None:
        self.retry_after = retry_after
        super().__init__(detail)


class QuotaExceeded(Exception):
    """Tenant is over its storage quota. Writes rejected, reads unaffected."""


# Hard ceiling on tracked keys. Bounded memory matters more than exact accounting
# for an unbounded number of tenants: see RateLimiter._evict.
MAX_TRACKED_KEYS = 10_000


@dataclass
class _Window:
    started: float
    count: int = 0


@dataclass
class RateLimiter:
    """Fixed-window per-key counter.

    Fixed window rather than sliding: a sliding window needs per-request
    timestamps, which is unbounded memory for an unbounded number of tenants.
    The cost is burstiness at the boundary, which for admission control is an
    acceptable trade — this exists to stop runaway clients, not to bill anyone.
    """
    limit: int
    window_s: int = 1
    _windows: dict[str, _Window] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _evict(self, now: float) -> None:
        """Sweep expired windows, then hard-cap.

        Sweeping alone is not enough, and the difference is a denial of service
        rather than a tidiness question: a burst of requests carrying many
        DISTINCT tenant ids within a single window leaves nothing expired to
        sweep, so the map grows without bound — and it grows inside the
        component whose whole job is to stop a client exhausting the server.
        After sweeping, the oldest windows are dropped to the cap. Dropping a
        live window only forgives a caller some requests; it never denies one.
        """
        cutoff = now - self.window_s
        for k in [k for k, v in self._windows.items() if v.started < cutoff]:
            self._windows.pop(k, None)
        if len(self._windows) > MAX_TRACKED_KEYS:
            oldest = sorted(self._windows.items(), key=lambda kv: kv[1].started)
            for k, _ in oldest[: len(self._windows) - MAX_TRACKED_KEYS]:
                self._windows.pop(k, None)

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            w = self._windows.get(key)
            if w is None or now - w.started >= self.window_s:
                self._windows[key] = _Window(started=now, count=1)
                if len(self._windows) > MAX_TRACKED_KEYS:
                    self._evict(now)
                return
            w.count += 1
            if w.count > self.limit:
                retry = max(1, int(self.window_s - (now - w.started)) + 1)
                raise RateLimited(
                    retry,
                    f"rate limit exceeded: {self.limit} requests per "
                    f"{self.window_s}s for this tenant")


_read_limiter: RateLimiter | None = None
_write_limiter: RateLimiter | None = None


def limiters() -> tuple[RateLimiter, RateLimiter]:
    global _read_limiter, _write_limiter
    if _read_limiter is None:
        s = settings()
        # Writes are capped harder than reads on purpose: a write costs an
        # embedding call and a synchronous index update, so the same request
        # rate is a very different amount of work.
        _read_limiter = RateLimiter(limit=s.rate_limit_read_rps, window_s=1)
        _write_limiter = RateLimiter(limit=s.rate_limit_write_rps, window_s=1)
    return _read_limiter, _write_limiter


def check_read(tenant_id: str) -> None:
    if settings().limits_enabled:
        limiters()[0].check(f"r:{tenant_id}")


def check_write(tenant_id: str) -> None:
    if settings().limits_enabled:
        limiters()[1].check(f"w:{tenant_id}")


def check_backpressure(conn: Connection) -> None:
    """Refuse new work when the queue is already too deep.

    Accepting a write whose embedding job will sit behind 50,000 others is
    dishonest: the caller is told the memory is stored and searchable, and it
    will not be searchable for hours. Better to refuse and say when to come back.
    """
    s = settings()
    if not s.limits_enabled or s.max_queue_depth <= 0:
        return
    try:
        depth = conn.execute(text(
            "SELECT count(*) FROM procrastinate_jobs WHERE status = 'todo'"
        )).scalar_one()
    except Exception:  # noqa: BLE001
        # No queue table (or no permission) is not a reason to refuse traffic.
        return
    if depth > s.max_queue_depth:
        raise Overloaded(30, f"job queue depth {depth} exceeds {s.max_queue_depth}; "
                             "shedding new writes until it drains")


def check_quota(conn: Connection, tenant_id: str) -> None:
    """Storage quota, enforced on writes only."""
    s = settings()
    if not s.limits_enabled or s.max_memories_per_tenant <= 0:
        return
    n = conn.execute(
        text("SELECT count(*) FROM mem.memories WHERE tenant_id = :t "
             "  AND status <> 'deleted'"),
        {"t": tenant_id},
    ).scalar_one()
    if n >= s.max_memories_per_tenant:
        raise QuotaExceeded(
            f"tenant holds {n} memories, quota is {s.max_memories_per_tenant}. "
            "Reads are unaffected. Archive or raise the quota to write more.")
