"""Entity extraction, canonicalisation and the graph arm.

The graph arm measured 0% of expected hits against the golden set, which reads
like a weak arm and was actually an absent one: mem.entities and
mem.entity_mentions were never written to. These tests exist so it cannot
silently become empty again — a retrieval arm with no data looks identical to a
retrieval arm that simply is not helping, and one of those is a bug.

    docker compose exec -T api python - < tests/test_entities.py
"""
from __future__ import annotations

import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, entities, memories  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("e17a0000-0000-0000-0000-0000000000e1")
PROJECT = UUID("e17a0000-0000-0000-0000-0000000000e2")
PRINCIPAL = UUID("e17a0000-0000-0000-0000-0000000000e3")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) VALUES (:i,'ent','Ent') "
                       "ON CONFLICT DO NOTHING"), {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,'ent-a','Ent A') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT)})
        c.execute(text("INSERT INTO mem.principals (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'agent',:e,'ent') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"ent-{PRINCIPAL}"})


def names(text_: str) -> set[str]:
    return {n for n, _, _ in entities.extract(text_)}


def main() -> None:
    seed()

    # ---- 1. canonicalisation ----------------------------------------------
    # The entire value of the dictionary: one node per real thing, however it
    # was spelled. Three spellings producing three nodes would split the most
    # connected entity in the project into disconnected copies.
    print("\n1. Canonicalisation (one node per real thing)")
    for spelling in ("Postgres", "PostgreSQL", "postgresql", "psql", "we use PG here"):
        check(f"{spelling!r:22} -> PostgreSQL", "PostgreSQL" in names(spelling),
              str(names(spelling))[:40])
    check("row level security -> RLS", "RLS" in names("we enforce row level security"))
    check("row-level security -> RLS", "RLS" in names("row-level security is on"))
    check("reciprocal rank fusion -> RRF", "RRF" in names("fused with reciprocal rank fusion"))

    # ---- 2. precision ------------------------------------------------------
    print("\n2. Precision (a false entity links unrelated memories forever)")
    check("no entities in ordinary prose",
          names("The team met on Tuesday to discuss the roadmap.") == set(),
          str(names("The team met on Tuesday to discuss the roadmap."))[:40])
    check("substrings do not match",
          "Ollama" not in names("the collama library"), str(names("the collama library")))
    check("hyphenated words do not match",
          "TEI" not in names("protein-folding"), str(names("protein-folding")))
    check("code paths are found",
          "memory_platform/db.py" in names("see memory_platform/db.py"),
          str(names("see memory_platform/db.py"))[:50])
    check("sql objects are found",
          "mem.fn_set_scope" in names("call mem.fn_set_scope first"))

    # ---- 3. weighting ------------------------------------------------------
    print("\n3. Weighting (a passing mention should not pull like a subject)")
    ex = dict((n, w) for n, _, w in entities.extract(
        "PgBouncer PgBouncer PgBouncer pooling. Also mentions Grafana once."))
    check("repeated entity outweighs a single mention",
          ex.get("PgBouncer", 0) > ex.get("Grafana", 1), str(ex))
    check("weights are normalised to <= 1.0", all(w <= 1.0 for w in ex.values()), str(ex))

    # ---- 4. linking + idempotency -----------------------------------------
    print("\n4. Linking a memory")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        r = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title="Use PgBouncer in transaction mode",
            content=("PgBouncer fronts the API only. Workers talk to PostgreSQL "
                     f"directly because Procrastinate needs LISTEN/NOTIFY. Run {RUN}."),
            source_type="git", memory_key=f"ent-{RUN}-pgb")
        linked = c.execute(text(
            "SELECT e.canonical_name FROM mem.entity_mentions em "
            "  JOIN mem.entities e ON e.id = em.entity_id "
            " WHERE em.memory_id = :m ORDER BY 1"), {"m": str(r["id"])}).scalars().all()
    check("write_memory links entities automatically",
          {"PgBouncer", "PostgreSQL", "Procrastinate"} <= set(linked), str(linked))

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        before = c.execute(text("SELECT count(*) FROM mem.entity_mentions "
                                " WHERE memory_id = :m"), {"m": str(r["id"])}).scalar_one()
        entities.link_memory(c, tenant_id=TENANT, project_id=PROJECT,
                             memory_id=r["id"], title="Use PgBouncer in transaction mode",
                             content=f"PgBouncer and PostgreSQL and Procrastinate. {RUN}")
        after = c.execute(text("SELECT count(*) FROM mem.entity_mentions "
                               " WHERE memory_id = :m"), {"m": str(r["id"])}).scalar_one()
    check("re-linking converges rather than duplicating", after == before,
          f"{before} -> {after}")

    # ---- 5. query resolution ----------------------------------------------
    print("\n5. Query resolution (same rules as the writer used)")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        ids = entities.resolve_query_entities(
            c, "why does PgBouncer break LISTEN/NOTIFY?",
            tenant_id=TENANT, project_id=PROJECT)
        check("query names resolve to entity ids", len(ids) >= 1, f"{len(ids)} ids")
        none = entities.resolve_query_entities(
            c, "an unrelated question about nothing in particular",
            tenant_id=TENANT, project_id=PROJECT)
        check("a query with no entities resolves to none", none == [], str(none))

    # ---- 6. the graph arm actually fires ----------------------------------
    print("\n6. Graph arm contributes (it measured 0% when tables were empty)")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        # A second memory sharing an entity but NOT the wording, so the graph arm
        # is the only arm that can plausibly connect them.
        memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="failure", title="Pooling outage during release",
            content=("The pooler refused every connection with a wrong password "
                     f"type after a credential rotation. PgBouncer. Run {RUN}."),
            source_type="ci", memory_key=f"ent-{RUN}-outage")

        ids = entities.resolve_query_entities(c, "PgBouncer",
                                              tenant_id=TENANT, project_id=PROJECT)
        rows = c.execute(text(
            "SELECT s.r_graph FROM mem.search_hybrid("
            "  CAST(:v AS halfvec(1024)), :q, NULL, CAST('observed' AS mem.trust_tier), "
            "  now(), CAST(:e AS uuid[]), 20, 60, "
            "  ARRAY['active']::mem.memory_status[]) s"),
            {"v": "[" + ",".join(["0.01"] * 1024) + "]", "q": "PgBouncer",
             "e": "{" + ",".join(ids) + "}"}).scalars().all()
        graph_hits = [r for r in rows if r]
        check("graph arm returns ranked rows for a known entity",
              len(graph_hits) >= 1, f"{len(graph_hits)} of {len(rows)}")

    # ---- 7. scope --------------------------------------------------------
    print("\n7. Entities are tenant-scoped like everything else")
    with db.engine().connect() as c:
        n = c.execute(text("SELECT count(*) FROM mem.entities")).scalar_one()
    check("entities invisible without a scope (RLS)", n == 0, f"saw {n}")

    # ---- 8. relationships (P7b) -------------------------------------------
    print("\n8. Relationship extraction")
    check("relation phrase between two entities yields an edge",
          entities.extract_relations("The API depends on PgBouncer for pooling.")
          == [("api", "depends_on", "PgBouncer")])
    check("a clause boundary stops a false edge",
          entities.extract_relations(
              "PgBouncer is fronted by nothing; Procrastinate uses PostgreSQL.")
          == [("Procrastinate", "uses", "PostgreSQL")])
    check("nearest entity wins, not the first in the clause",
          entities.extract_relations(
              "Grafana and Prometheus are separate; the worker uses Procrastinate.")
          == [("worker", "uses", "Procrastinate")])
    check("co-mention without a relation phrase yields nothing",
          entities.extract_relations(
              "PostgreSQL and Grafana and Docker all appear here.") == [])
    check("rejected alternatives become `contradicts`",
          entities.extract_relations("We chose PostgreSQL instead of Qdrant.")
          == [("PostgreSQL", "contradicts", "Qdrant")])

    print("\n9. Trusted sources assert edges; untrusted ones propose them")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        trusted = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title="Pooling topology",
            content=f"The API depends on PgBouncer for pooling. Run {RUN}.",
            source_type="git", memory_key=f"rel-trusted-{RUN}")
        untrusted = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="observation", title="Agent guess about topology",
            content=f"The worker uses Procrastinate probably. Run {RUN}.",
            source_type="agent", memory_key=f"rel-untrusted-{RUN}")
        # Assert the EDGE exists between the right entities — not that this run's
        # memory is its evidence. relationships is UNIQUE on
        # (tenant, source, target, relation, valid_at WITHOUT OVERLAPS), so a
        # second memory asserting the same fact correctly does nothing and
        # evidence_memory_id keeps pointing at whichever memory said it first.
        # The earlier assertion passed only on a database that had never seen
        # this edge before, which is a test that works exactly once.
        def edge(table, src, rel, dst):
            return c.execute(text(
                f"SELECT count(*) FROM mem.{table} r "
                "  JOIN mem.entities s ON s.id = r.source_id "
                "  JOIN mem.entities d ON d.id = r.target_id "
                " WHERE s.canonical_name = :s AND d.canonical_name = :d "
                "   AND r.relation::text = :r"),
                {"s": src, "d": dst, "r": rel}).scalar_one()

        n_edges = edge("relationships", "api", "depends_on", "PgBouncer")
        n_prop = edge("proposed_relationships", "worker", "uses", "Procrastinate")
        n_bad = edge("relationships", "worker", "uses", "Procrastinate")
        tier = c.execute(text(
            "SELECT r.tier::text FROM mem.relationships r "
            "  JOIN mem.entities s ON s.id = r.source_id "
            " WHERE s.canonical_name = 'api' LIMIT 1")).scalar_one_or_none()
    check("a git-sourced memory creates a real edge", n_edges == 1, str(n_edges))
    check("the edge is tier `observed`, never higher", tier == "observed", str(tier))
    check("an agent-sourced memory only PROPOSES one", n_prop >= 1, str(n_prop))
    check("and creates no real edge (§444: edges from tier >= 2 only)",
          n_bad == 0, str(n_bad))

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
