"""Admission control — rate limits, quota, backpressure (Phase 9).

The property that matters most is that the three failure modes stay distinct:

  429 rate limit  -> "you are going too fast"      (client slows down)
  429 quota       -> "you are storing too much"    (client stops writing)
  503 overload    -> "we are behind"               (client retries later)

A client that receives the wrong one behaves wrongly: it backs off from a
transient blip, or it hammers a limit that will never clear on its own.

    docker compose exec -T api python - < tests/test_limits.py
"""
from __future__ import annotations

import sys
import time
import uuid

sys.path.insert(0, "/app/src")
from memory_platform import limits  # noqa: E402
from memory_platform.config import settings  # noqa: E402

RUN = uuid.uuid4().hex[:8]

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def main() -> None:
    # ---- 1. the limiter itself --------------------------------------------
    print("\n1. Fixed-window rate limiter")
    rl = limits.RateLimiter(limit=3, window_s=1)
    for i in range(3):
        rl.check("t1")
    try:
        rl.check("t1")
        check("the 4th request in a 3/s window is rejected", False, "allowed")
    except limits.RateLimited as exc:
        check("the 4th request in a 3/s window is rejected", True)
        check("rejection carries a Retry-After", exc.retry_after >= 1, str(exc.retry_after))
        check("the message says what the limit is", "3 requests" in str(exc), str(exc)[:50])

    # ---- 2. tenants are isolated ------------------------------------------
    print("\n2. One tenant's traffic does not throttle another")
    try:
        rl.check("t2")
        rl.check("t2")
        check("a different tenant has its own window", True)
    except limits.RateLimited:
        check("a different tenant has its own window", False, "cross-tenant throttle")

    # ---- 3. the window actually resets ------------------------------------
    print("\n3. The window resets")
    time.sleep(1.1)
    try:
        rl.check("t1")
        check("after the window elapses, requests are allowed again", True)
    except limits.RateLimited:
        check("after the window elapses, requests are allowed again", False)

    # ---- 4. no unbounded memory growth ------------------------------------
    print("\n4. Per-key state does not grow without bound")
    big = limits.RateLimiter(limit=100, window_s=1)
    for i in range(12_000):
        big.check(f"tenant-{i}")
    # Sweeping alone does not bound this: 12,000 distinct keys inside one window
    # leaves nothing expired to remove. The hard cap is what makes it safe, and
    # the first version of this limiter had only the sweep.
    check("tracked keys are hard-capped, not merely swept",
          len(big._windows) <= limits.MAX_TRACKED_KEYS,
          f"{len(big._windows)} keys, cap {limits.MAX_TRACKED_KEYS}")
    check("eviction never denies a request",
          True)  # 12,000 checks above all returned without raising

    # ---- 5. disabled by default -------------------------------------------
    print("\n5. Off unless explicitly enabled")
    check("limits are disabled by default",
          settings().limits_enabled is False, str(settings().limits_enabled))
    # With limits off these must be no-ops however hard they are called.
    for _ in range(200):
        limits.check_read("some-tenant")
        limits.check_write("some-tenant")
    check("check_read/check_write are no-ops when disabled", True)

    # ---- 6. the exception types are distinct ------------------------------
    print("\n6. Three failure modes, three exception types")
    check("RateLimited is not Overloaded",
          not issubclass(limits.RateLimited, limits.Overloaded))
    check("QuotaExceeded is its own type",
          not issubclass(limits.QuotaExceeded, limits.RateLimited))
    check("Overloaded carries a retry hint",
          hasattr(limits.Overloaded(30, "x"), "retry_after"))

    # ---- 7. write limit is tighter than read ------------------------------
    print("\n7. Writes are capped harder than reads")
    s = settings()
    check("write rps <= read rps (a write costs an embedding call)",
          s.rate_limit_write_rps <= s.rate_limit_read_rps,
          f"{s.rate_limit_write_rps} vs {s.rate_limit_read_rps}")

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
