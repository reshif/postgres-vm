"""Console read-model contract tests against the running API.

Explorer, Timeline, and Graph are all read-only views over the same RLS scope as
MCP. This suite seeds a tiny bi-temporal graph, then proves the HTTP API never
turns a historical cursor into a client-side filtering exercise.

    docker compose exec -T api python - < tests/test_console_data.py
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, memories  # noqa: E402


API = "http://localhost:8080"
RUN = uuid.uuid4().hex[:8]
TENANT = UUID(f"c0de{RUN[:4]}-0000-4000-8000-000000000001")
PROJECT = UUID(f"c0de{RUN[:4]}-0000-4000-8000-000000000002")
PRINCIPAL = UUID(f"c0de{RUN[:4]}-0000-4000-8000-000000000003")
KEY = f"console-history-{RUN}"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def request_json(method: str, path: str, body: dict | None = None, **params) -> tuple[int, dict]:
    query = urllib.parse.urlencode({key: str(value) for key, value in params.items()
                                    if value is not None})
    request = urllib.request.Request(
        f"{API}{path}?{query}",
        data=json.dumps(body, default=str).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.status, json.load(response)


def api(path: str, **params) -> dict:
    _, payload = request_json("GET", path, **params)
    return payload


def seed() -> tuple[dict, dict, UUID, UUID, datetime]:
    with db.engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO mem.organizations (id, slug, name) VALUES (:id, :slug, 'Console') "
            "ON CONFLICT DO NOTHING"), {"id": str(TENANT), "slug": f"console-{RUN}"})
        conn.execute(text(
            "INSERT INTO mem.projects (id, tenant_id, slug, name) "
            "VALUES (:id, :tenant, :slug, 'Console fixture') ON CONFLICT DO NOTHING"),
            {"id": str(PROJECT), "tenant": str(TENANT), "slug": f"console-{RUN}"})
        conn.execute(text(
            "INSERT INTO mem.principals (id, tenant_id, actor, external_id, display_name) "
            "VALUES (:id, :tenant, 'agent', :external, 'Console fixture') ON CONFLICT DO NOTHING"),
            {"id": str(PRINCIPAL), "tenant": str(TENANT), "external": f"console-{RUN}"})

    now = datetime.now(timezone.utc)
    before = now - timedelta(days=30)
    switch = now - timedelta(days=10)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        old = memories.write_memory(
            conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title=f"PostgreSQL 15 for {RUN}",
            content=f"PostgreSQL uses Redis for the first design. Fixture {RUN}.",
            source_type="git", memory_key=KEY)
        conn.execute(text(
            "UPDATE mem.memories SET recorded_at = :at, valid_at = tstzrange(:at, NULL, '[)') "
            "WHERE id = :id"), {"at": before, "id": str(old["id"])})
        current = memories.write_memory(
            conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title=f"PostgreSQL 17 for {RUN}",
            content=f"PostgreSQL uses Redis after the upgrade. Fixture {RUN}.",
            source_type="git", memory_key=KEY)
        conn.execute(text(
            "UPDATE mem.memories SET valid_at = tstzrange(lower(valid_at), :switch, '[)') "
            "WHERE id = :id"), {"switch": switch, "id": str(old["id"])})
        conn.execute(text(
            "UPDATE mem.memories SET recorded_at = :switch, valid_at = tstzrange(:switch, NULL, '[)') "
            "WHERE id = :id"), {"switch": switch, "id": str(current["id"])})
        candidate = memories.write_memory(
            conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="observation", title=f"Unreviewed graph observation {RUN}",
            content=f"PostgreSQL uses Redis in an unreviewed observation. Fixture {RUN}.",
            source_type="agent", memory_key=f"console-proposed-{RUN}")
        procedure = memories.write_memory(
            conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="procedure", title=f"Deploy the console fixture {RUN}",
            content=f"Run the verified console deployment procedure. Fixture {RUN}.",
            source_type="git", memory_key=f"console-procedure-{RUN}")
        # Dashboard demand must come from retrieval_events rather than waiting for
        # the scheduler to denormalise retrieval_count on the memory rows.
        event_columns = """
            tenant_id, project_id, principal_id, pack_id, tool, query_text,
            plan, arm_results, fused, dropped, returned_ids, token_count,
            ranking_profile, latency_ms, created_at
        """
        event_common = {
            "tenant": str(TENANT), "project": str(PROJECT), "principal": str(PRINCIPAL),
            "arms": "{}", "fused": "[]", "dropped": "[]", "profile": "default@2",
            "latency": "{}", "created": now,
        }
        conn.execute(text(
            f"INSERT INTO mem.retrieval_events ({event_columns}) VALUES "
            "(:tenant, :project, :principal, :pack, 'memory_context', :query, "
            "CAST(:plan AS jsonb), CAST(:arms AS jsonb), CAST(:fused AS jsonb), "
            "CAST(:dropped AS jsonb), CAST(:returned_ids AS uuid[]), 120, :profile, "
            "CAST(:latency AS jsonb), :created)"),
            {**event_common, "pack": f"dashboard-supported-{RUN}",
             "query": f"Why use PostgreSQL {RUN}?",
             "plan": json.dumps({"answerability": {"status": "supported"}}),
             "returned_ids": "{" + str(current["id"]) + "}"})
        conn.execute(text(
            f"INSERT INTO mem.retrieval_events ({event_columns}) VALUES "
            "(:tenant, :project, :principal, :pack, 'memory_context', :query, "
            "CAST(:plan AS jsonb), CAST(:arms AS jsonb), CAST(:fused AS jsonb), "
            "CAST(:dropped AS jsonb), CAST(:returned_ids AS uuid[]), 80, :profile, "
            "CAST(:latency AS jsonb), :created)"),
            {**event_common, "pack": f"dashboard-gap-{RUN}",
             "query": f"What is the missing runbook {RUN}?",
             "plan": json.dumps({"answerability": {"status": "no_relevant_evidence"}}),
             "returned_ids": "{}"})
        rows = conn.execute(text(
            "SELECT id, canonical_name FROM mem.entities "
            "WHERE tenant_id = :tenant AND project_id = :project "
            "AND canonical_name = ANY(:names)"),
            {"tenant": str(TENANT), "project": str(PROJECT),
             "names": ["PostgreSQL", "Redis"]}).mappings().all()
        entities = {row["canonical_name"]: UUID(str(row["id"])) for row in rows}
    return old, current, entities["PostgreSQL"], entities["Redis"], switch, procedure, candidate


def main() -> None:
    old, current, postgres, redis, switch, procedure, candidate = seed()
    scope = {"tenant_id": TENANT, "project_id": PROJECT, "principal_id": PRINCIPAL}

    print("\n1. Knowledge Explorer")
    present = api("/v1/explorer", **scope, q=RUN, limit=20)
    present_ids = {item["id"] for item in present["items"]}
    check("the explorer reports the current version", str(current["id"]) in present_ids,
          str(present["total"]))
    check("the explorer excludes a superseded current version", str(old["id"]) not in present_ids)

    historical = api("/v1/explorer", **scope, q=RUN,
                     as_of=(switch - timedelta(days=1)).isoformat(), limit=20)
    historical_ids = {item["id"] for item in historical["items"]}
    check("the explorer changes with its as-of cursor",
          str(old["id"]) in historical_ids and str(current["id"]) not in historical_ids,
          str(historical.get("as_of")))
    check("explorer rows expose the table's operational columns",
          {"tier", "type", "source_uri", "valid_from", "last_accessed_at",
           "retrieval_count", "token_cost", "status"}.issubset(present["items"][0]),
          ",".join(sorted(present["items"][0])))
    detail = api("/v1/explain", **scope, ref=current["id"])
    check("memory detail includes full content and usage evidence",
          "Fixture" in detail["memory"]["content"]
          and {"retrievals", "packs", "principals", "last_seen"}.issubset(detail["usage"]),
          str(detail["usage"]))

    print("\n2. Bi-temporal Timeline")
    timed = api("/v1/timeline", **scope, as_of=(switch - timedelta(days=1)).isoformat())
    valid_ids = {item["id"] for item in timed["valid_lane"] if item["active_at_as_of"]}
    recorded_ids = {item["id"] for item in timed["recorded_lane"]}
    check("the valid-time lane contains the belief that was active then",
          str(old["id"]) in valid_ids and str(current["id"]) not in valid_ids)
    check("the recorded-time lane excludes knowledge learned later",
          str(old["id"]) in recorded_ids and str(current["id"]) not in recorded_ids)

    print("\n3. Entity Graph")
    suggestions = api("/v1/graph", **scope, q="PostgreSQL")
    check("an unfocused graph returns suggestions instead of the whole graph",
          not suggestions["nodes"] and not suggestions["edges"]
          and any(item["id"] == str(postgres) for item in suggestions["suggestions"]),
          str(len(suggestions["suggestions"])))

    focused = api("/v1/graph", **scope, entity_id=postgres)
    node_ids = {item["id"] for item in focused["nodes"]}
    connected = [edge for edge in focused["edges"]
                 if {edge["source_id"], edge["target_id"]} == {str(postgres), str(redis)}]
    check("a focused graph is a bounded neighbourhood with both entity nodes",
          {str(postgres), str(redis)}.issubset(node_ids), str(len(node_ids)))
    check("graph edges carry relation, trust, confidence, provenance, and valid time",
          bool(connected) and {"relation", "tier", "confidence", "evidence_memory_id",
                               "valid_from", "valid_until"}.issubset(connected[0]),
          str(connected[:1]))
    check("unreviewed graph edges are explicit proposed data, not observed facts",
          any(edge["proposed"] for edge in focused["edges"]),
          str([(edge["relation"], edge["proposed"]) for edge in focused["edges"]]))

    print("\n4. Knowledge demand dashboard")
    dashboard = api("/v1/dashboard", **scope, days=30)
    outcomes = {item["status"]: item["count"] for item in dashboard["outcomes"]}
    asked = {item["query_text"]: item for item in dashboard["top_questions"]}
    returned = {item["id"]: item for item in dashboard["top_knowledge"]}
    check("dashboard exposes scoped retrieval demand immediately",
          dashboard["summary"]["requests"] >= 2
          and dashboard["summary"]["questions"] >= 2
          and sum(item["requests"] for item in dashboard["trend"]) >= 2,
          str(dashboard["summary"]))
    check("dashboard identifies a repeated question and its evidence outcome",
          asked.get(f"Why use PostgreSQL {RUN}?", {}).get("answerability") == "supported"
          and outcomes.get("no_relevant_evidence") == 1,
          str(outcomes))
    check("dashboard identifies the knowledge actually returned for a question",
          returned.get(str(current["id"]), {}).get("requests") == 1,
          str(dashboard["top_knowledge"]))

    print("\n5. Saved Explorer Views")
    saved_filters = {"tiers": ["inferred", "untrusted"], "statuses": ["quarantined"]}
    status, saved = request_json("POST", "/v1/console/views", {
        **scope, "name": "Contested", "filters": saved_filters,
    })
    saved_id = saved.get("id")
    check("a named project filter can be saved", status == 201 and bool(saved_id)
          and saved["filters"] == saved_filters, str(saved))

    listed = api("/v1/console/views", **scope)
    check("saved views are listed within their project scope",
          any(item["id"] == saved_id and item["name"] == "Contested"
              for item in listed["views"]), str(listed))

    changed_filters = {"never_retrieved": True}
    status, updated = request_json("POST", "/v1/console/views", {
        **scope, "name": "Contested", "filters": changed_filters,
    })
    check("saving the same named view updates its filters", status == 201
          and updated["id"] == saved_id and updated["filters"] == changed_filters,
          str(updated))

    status, deleted = request_json("DELETE", f"/v1/console/views/{saved_id}",
                                   **scope)
    check("saved views can be removed", status == 200 and deleted.get("deleted") is True,
          str(deleted))
    after_delete = api("/v1/console/views", **scope)
    check("a removed view no longer appears", all(item["id"] != saved_id
          for item in after_delete["views"]), str(after_delete))

    print("\n6. Procedures, settings, and audit")
    procedure_rows = api("/v1/procedures", **scope)
    check("procedures are listed within their project scope",
          any(item["id"] == str(procedure["id"]) for item in procedure_rows["procedures"]),
          str(procedure_rows))

    configured = api("/v1/console/settings", **scope)
    check("settings returns the scoped project and active ranking profile",
          configured["project"]["id"] == str(PROJECT)
          and bool(configured.get("ranking_profile")), str(configured)[:180])

    status, pinned = request_json("POST", "/v1/console/memories/actions", {
        **scope, "refs": [current["id"]], "action": "pin", "reason": "operational priority",
    })
    check("console can pin an in-scope memory with an audited action",
          status == 200 and pinned["memories"][0]["pinned"] is True, str(pinned))
    status, unpinned = request_json("POST", "/v1/console/memories/actions", {
        **scope, "refs": [current["id"]], "action": "unpin",
    })
    check("console can remove a pin without deleting the memory",
          status == 200 and unpinned["memories"][0]["pinned"] is False, str(unpinned))
    status, reembedded = request_json("POST", "/v1/console/memories/actions", {
        **scope, "refs": [current["id"]], "action": "reembed",
    })
    check("console re-embedding refreshes the active vector model",
          status == 200 and reembedded["memories"][0].get("embedded") is True,
          str(reembedded))

    status, _ = request_json("POST", "/v1/inbox/review", {
        **scope, "ref": candidate["id"], "action": "promote", "to_tier": "observed",
    })
    archive_status, archived = request_json("POST", "/v1/console/memories/actions", {
        **scope, "refs": [candidate["id"]], "action": "archive", "reason": "fixture cleanup",
    })
    audit = api("/v1/audit", **scope)
    check("project audit returns reviewed and lifecycle decisions",
          status == 200 and any(event["action"] == "review.promote"
                                for event in audit["events"])
          and archive_status == 200 and archived["memories"][0]["status"] == "archived"
          and any(event["action"] == "console.memory.archive" for event in audit["events"]),
          str(audit))

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
