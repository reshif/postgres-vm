"""Per-arm contribution to returned packs — the ADR-0008 keep-or-cut measurement.

ADR-0008, and 05-BUILD-PLAN Phase 7:

    Each arm's contribution must be measured; an arm contributing under ~3% of
    returned items over a month should be removed rather than tuned.

That sentence is a commitment to delete code, which makes the measurement behind
it load-bearing. Two things follow.

**Candidate counts cannot answer it.** `retrieval_events.arm_results` records how
many rows each arm produced, and an arm can produce twenty-five candidates on
every single query while being responsible for nothing that survives fusion.
The blueprint says RETURNED ITEMS, so contribution is measured over
`returned_ids` using the per-item arm attribution stored in `fused`.

**Two different numbers, and the difference is the decision.**

  participation — the arm surfaced this item, possibly alongside others.
  unique        — the arm was the ONLY one that surfaced it.

Participation flatters every arm: the vector arm surfaces almost everything, so
anything it also finds looks well-supported. Unique contribution is the honest
form of "what would we lose by cutting this", and it is the one compared against
the floor. Both are reported, because an arm with high participation and zero
unique contribution is not merely weak — it is redundant, which is a different
fix from tuning.

WHY THE REPORT INSISTS ON SAYING HOW MUCH EVIDENCE IT HAS. Events written before
per-item attribution existed carry no `arms` key, and an average over three
events is not a month of production behaviour. Reporting a confident-looking
percentage from either would invite exactly the irreversible decision this
measurement is meant to inform, so coverage and sample size are part of the
result and the verdict is withheld until both are adequate.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .config import settings
from .context import ARM_RANK_KEYS

log = logging.getLogger("memory.arms")

ARMS = tuple(arm for arm, _ in ARM_RANK_KEYS)


def contribution(conn: Connection, *, tenant_id: UUID, project_id: UUID | None = None,
                 days: int | None = None) -> dict[str, Any]:
    """Measure what each retrieval arm contributed to returned packs.

    Returns per-arm participation and unique-contribution shares over the window,
    plus enough context to tell a real signal from an empty one.
    """
    cfg = settings()
    window = int(days if days is not None else cfg.arm_contribution_window_days)
    floor = float(cfg.arm_contribution_floor)
    min_events = int(cfg.arm_contribution_min_events)

    params: dict[str, Any] = {"t": str(tenant_id), "days": window}
    project_clause = ""
    if project_id is not None:
        project_clause = " AND project_id = :p"
        params["p"] = str(project_id)

    # One row per (event, returned item), carrying the arms that surfaced it.
    # Restricted to items that were actually RETURNED: `fused` holds the top 40
    # scored candidates, and counting an arm for something that never reached the
    # pack would measure candidate generation again under a different name.
    rows = conn.execute(
        text(
            "WITH ev AS ("
            "  SELECT id, returned_ids, fused FROM mem.retrieval_events "
            "   WHERE tenant_id = :t"
            f"{project_clause}"
            "     AND created_at >= now() - make_interval(days => :days)), "
            "item AS ("
            "  SELECT ev.id AS event_id, f ->> 'id' AS memory_id, "
            "         f -> 'arms' AS arms "
            "    FROM ev, LATERAL jsonb_array_elements(ev.fused) AS f "
            "   WHERE f ? 'arms' "
            "     AND (f ->> 'id')::uuid = ANY(ev.returned_ids)) "
            "SELECT event_id, memory_id, arms FROM item"),
        params).mappings().all()

    total_events = conn.execute(
        text("SELECT count(*) FROM mem.retrieval_events "
             " WHERE tenant_id = :t"
             f"{project_clause}"
             "   AND created_at >= now() - make_interval(days => :days)"),
        params).scalar_one()

    events_with_attribution = len({r["event_id"] for r in rows})
    items = len(rows)

    counts = {arm: {"participated": 0, "unique": 0} for arm in ARMS}
    for row in rows:
        arms = [a for a in (row["arms"] or []) if a in counts]
        for arm in arms:
            counts[arm]["participated"] += 1
        if len(arms) == 1:
            counts[arms[0]]["unique"] += 1

    # Coverage is what stops a partially-migrated window from being read as a
    # verdict: a month in which only yesterday's events carry attribution is
    # yesterday's measurement wearing a month's label.
    coverage = (events_with_attribution / total_events) if total_events else 0.0
    sufficient = events_with_attribution >= min_events and items > 0

    report: dict[str, Any] = {
        "window_days": window,
        "floor": floor,
        "events": total_events,
        "events_with_attribution": events_with_attribution,
        "attribution_coverage": round(coverage, 4),
        "returned_items": items,
        "min_events": min_events,
        "sufficient_evidence": sufficient,
        "arms": {},
    }
    for arm in ARMS:
        participated = counts[arm]["participated"]
        unique = counts[arm]["unique"]
        share = (participated / items) if items else 0.0
        unique_share = (unique / items) if items else 0.0
        report["arms"][arm] = {
            "participated": participated,
            "unique": unique,
            "share": round(share, 4),
            "unique_share": round(unique_share, 4),
            # The verdict is the point of the whole module, so it is stated
            # rather than left to be re-derived by every reader. It is withheld
            # — not defaulted to "cut" — when the evidence is thin, because the
            # action it recommends is deleting a retrieval arm.
            "verdict": ("insufficient_evidence" if not sufficient
                        else "cut" if unique_share < floor
                        else "keep"),
        }
    return report


def publish(conn: Connection, *, tenant_id: UUID, project_id: UUID,
            project_label: str) -> dict[str, Any]:
    """Compute the report and publish it as gauges for the dashboard and alert."""
    from . import metrics as _metrics

    report = contribution(conn, tenant_id=tenant_id, project_id=project_id)
    _metrics.publish_arm_contribution(project_label, report)
    for arm, stats in report["arms"].items():
        if stats["verdict"] == "cut":
            log.info(
                "arm %s contributed %.2f%% of returned items uniquely over %d days "
                "(floor %.1f%%) — ADR-0008 says remove rather than tune",
                arm, stats["unique_share"] * 100, report["window_days"],
                report["floor"] * 100)
    return report
