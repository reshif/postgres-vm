"""Curator instrumentation and the ADR-0015 kill switch.

05-BUILD-PLAN.md Phase 5 states the numbers exactly:

    "inbox depth per project, weekly review minutes, alert at depth 100,
     automatic disable of LLM extraction at depth 200 sustained for two weeks.
     The kill switch is tested by simulation BEFORE extraction is enabled,
     not after."

WHY A KILL SWITCH AT ALL. ADR-0015's finding is that the thing that fails is not
the extractor and not the queue — it is the human. Week one the inbox gets
triaged daily; week nine nobody opens it. An extractor writing into a queue no
one reads is not "capturing knowledge", it is manufacturing a backlog that will
eventually be bulk-accepted by someone who wants it gone, and bulk acceptance is
how machine-written guesses become trusted memory.

So extraction is not merely discouraged when curation stops. It stops too.

WHY "SUSTAINED", AND WHY DAILY. A queue at 250 today may have been at 12
yesterday — a burst, not neglect, and disabling extraction for it punishes a team
that is actually working. The switch therefore requires EVERY daily sample across
the window to sit at or above the threshold, and requires the window to be
covered by real samples. A gap in sampling is not evidence of a healthy queue,
so a window that is not fully covered does not trip the switch either; it says
so, which is a different and honest answer.

WHY THE SWITCH RE-ARMS ITSELF. One day's sample below the threshold clears it.
Recovery must not need an admin — a switch that needs a human to reset is one
more thing for the same overloaded human to do, and it would be off for months.

ACCEPTANCE RATE, the second instrument. The 30-85% band is two failures with one
number between them. Under 30%: the extractor is noise and the reviewer is doing
unpaid cleanup. Over 85%: nobody is really reading — a reviewer who rejects
almost nothing has stopped reviewing and started clicking. Neither is visible
from any single decision, only from the ratio.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

log = logging.getLogger("memory.curation")

# ADR-0015 thresholds. Configurable because a 3-person team and a 300-person one
# do not have the same triage capacity, but the defaults are the plan's numbers.
ALERT_DEPTH = int(os.getenv("MEMORY_CURATION_ALERT_DEPTH", "100"))
DISABLE_DEPTH = int(os.getenv("MEMORY_CURATION_DISABLE_DEPTH", "200"))
SUSTAINED_DAYS = int(os.getenv("MEMORY_CURATION_SUSTAINED_DAYS", "14"))

# The acceptance band, both ends of which are failures.
ACCEPT_MIN = float(os.getenv("MEMORY_ACCEPT_RATE_MIN", "0.30"))
ACCEPT_MAX = float(os.getenv("MEMORY_ACCEPT_RATE_MAX", "0.85"))


def snapshot(conn: Connection, *, tenant_id: UUID, project_id: UUID) -> dict[str, Any]:
    """Record today's curation sample. Idempotent within a day.

    Called by the scheduler. Upserts rather than appends so a 60-second poll
    costs one row per project per day.
    """
    depth, oldest = conn.execute(
        text("SELECT count(*)::int, "
             "       COALESCE(MAX(EXTRACT(DAY FROM now() - recorded_at))::int, 0) "
             "  FROM mem.memories "
             " WHERE tenant_id = :t AND project_id = :p "
             "   AND status = 'quarantined' AND upper(valid_at) IS NULL"),
        {"t": str(tenant_id), "p": str(project_id)}).one()

    # Review throughput for today, straight from the audit log — the review
    # actions are already recorded there, so this needs no second write path
    # that could disagree with the first.
    promoted, rejected = conn.execute(
        text("SELECT "
             "  count(*) FILTER (WHERE action = 'review.promote')::int, "
             "  count(*) FILTER (WHERE action = 'review.reject')::int "
             "  FROM mem.audit_log "
             " WHERE tenant_id = :t AND created_at >= current_date"),
        {"t": str(tenant_id)}).one()

    extracted = conn.execute(
        text("SELECT count(*)::int FROM mem.memories "
             " WHERE tenant_id = :t AND project_id = :p "
             "   AND recorded_at >= current_date "
             "   AND metadata -> 'extraction' IS NOT NULL"),
        {"t": str(tenant_id), "p": str(project_id)}).scalar_one()

    conn.execute(
        text("INSERT INTO mem.curation_metrics "
             "  (tenant_id, project_id, observed_on, inbox_depth, oldest_days, "
             "   promoted, rejected, extracted) "
             "VALUES (:t, :p, current_date, :d, :o, :pr, :rj, :ex) "
             "ON CONFLICT (tenant_id, project_id, observed_on) DO UPDATE SET "
             "  inbox_depth = EXCLUDED.inbox_depth, "
             "  oldest_days = EXCLUDED.oldest_days, "
             "  promoted = EXCLUDED.promoted, rejected = EXCLUDED.rejected, "
             "  extracted = EXCLUDED.extracted, sampled_at = now()"),
        {"t": str(tenant_id), "p": str(project_id), "d": depth, "o": oldest,
         "pr": promoted, "rj": rejected, "ex": extracted})

    return {"inbox_depth": depth, "oldest_days": oldest,
            "promoted": promoted, "rejected": rejected, "extracted": extracted}


def extraction_allowed(
    conn: Connection, *, tenant_id: UUID, project_id: UUID,
) -> tuple[bool, str]:
    """The kill switch. Returns (allowed, reason) — the reason is never empty.

    A boolean alone would leave an operator staring at a silent extractor with no
    way to tell "disabled by policy" from "broken". The reason is what makes the
    switch debuggable, so it is part of the return type rather than a log line.
    """
    rows = conn.execute(
        text("SELECT observed_on, inbox_depth FROM mem.curation_metrics "
             " WHERE tenant_id = :t AND project_id = :p "
             "   AND observed_on > current_date - CAST(:d AS integer) "
             " ORDER BY observed_on DESC"),
        {"t": str(tenant_id), "p": str(project_id), "d": SUSTAINED_DAYS},
    ).mappings().all()

    if len(rows) < SUSTAINED_DAYS:
        # Not enough history to claim neglect. Fail OPEN here, deliberately: a
        # new project has no samples at all, and refusing to extract until it has
        # sampled for two weeks would disable the feature for exactly the
        # projects that just enabled it.
        return True, (f"insufficient history ({len(rows)}/{SUSTAINED_DAYS} daily "
                      "samples) — cannot establish a sustained backlog")

    below = [r for r in rows if r["inbox_depth"] < DISABLE_DEPTH]
    if below:
        return True, (f"backlog recovered on {below[0]['observed_on']} "
                      f"(depth {below[0]['inbox_depth']} < {DISABLE_DEPTH})")

    return False, (f"extraction disabled: inbox depth stayed at or above "
                   f"{DISABLE_DEPTH} for {SUSTAINED_DAYS} consecutive days "
                   f"(ADR-0015). Triage the inbox; the switch re-arms itself on "
                   f"the first day below the threshold.")


def acceptance_rate(
    conn: Connection, *, tenant_id: UUID, project_id: UUID, days: int = 28,
) -> dict[str, Any]:
    """Extraction acceptance over a window, with the 30-85% band applied."""
    promoted, rejected, extracted = conn.execute(
        text("SELECT COALESCE(SUM(promoted),0)::int, "
             "       COALESCE(SUM(rejected),0)::int, "
             "       COALESCE(SUM(extracted),0)::int "
             "  FROM mem.curation_metrics "
             " WHERE tenant_id = :t AND project_id = :p "
             "   AND observed_on > current_date - CAST(:d AS integer)"),
        {"t": str(tenant_id), "p": str(project_id), "d": days}).one()

    decided = promoted + rejected
    rate = (promoted / decided) if decided else None

    if rate is None:
        band = "no decisions in window"
    elif rate < ACCEPT_MIN:
        band = (f"below band ({rate:.0%} < {ACCEPT_MIN:.0%}) — the extractor is "
                "generating work rather than knowledge")
    elif rate > ACCEPT_MAX:
        band = (f"above band ({rate:.0%} > {ACCEPT_MAX:.0%}) — near-total "
                "acceptance usually means review has become a formality")
    else:
        band = f"in band ({rate:.0%})"

    return {"promoted": promoted, "rejected": rejected, "extracted": extracted,
            "decided": decided, "rate": rate, "band": band, "days": days,
            # Undecided proposals are the queue the band cannot see. An extractor
            # at 100% acceptance on 3 decisions and 400 pending is not healthy.
            "pending": max(extracted - decided, 0)}


def status(conn: Connection, *, tenant_id: UUID, project_id: UUID) -> dict[str, Any]:
    """Everything an operator needs about the curation loop, in one call."""
    cur = conn.execute(
        text("SELECT inbox_depth, oldest_days, observed_on FROM mem.curation_metrics "
             " WHERE tenant_id = :t AND project_id = :p "
             " ORDER BY observed_on DESC LIMIT 1"),
        {"t": str(tenant_id), "p": str(project_id)}).mappings().one_or_none()

    depth = cur["inbox_depth"] if cur else 0
    allowed, reason = extraction_allowed(conn, tenant_id=tenant_id, project_id=project_id)

    alerts: list[str] = []
    if depth >= DISABLE_DEPTH:
        alerts.append(f"inbox depth {depth} at or above the disable threshold "
                      f"({DISABLE_DEPTH})")
    elif depth >= ALERT_DEPTH:
        alerts.append(f"inbox depth {depth} past the alert threshold ({ALERT_DEPTH})")
    if cur and cur["oldest_days"] >= 14:
        alerts.append(f"oldest unreviewed item is {cur['oldest_days']} days old")

    acc = acceptance_rate(conn, tenant_id=tenant_id, project_id=project_id)
    if acc["rate"] is not None and not (ACCEPT_MIN <= acc["rate"] <= ACCEPT_MAX):
        alerts.append(f"acceptance rate {acc['band']}")

    return {
        "inbox_depth": depth,
        "oldest_days": cur["oldest_days"] if cur else 0,
        "sampled_on": str(cur["observed_on"]) if cur else None,
        "extraction_allowed": allowed,
        "extraction_reason": reason,
        "acceptance": acc,
        "alerts": alerts,
        "thresholds": {"alert": ALERT_DEPTH, "disable": DISABLE_DEPTH,
                       "sustained_days": SUSTAINED_DAYS,
                       "accept_band": [ACCEPT_MIN, ACCEPT_MAX]},
    }
