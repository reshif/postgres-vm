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

    # ---- authored domain knowledge: glossary -> graph ---------------------
    #
    # `.memory/glossary.md` was ingested at authoritative tier and described in
    # ingest.py as "entities and their canonical names" — while feeding only
    # searchable TEXT. `Context pack` and `Trust lattice` were defined there and
    # were not graph nodes. This is the bridge, and these tests are what stop it
    # silently reverting to a text-only path.
    print("\n10. Authored glossary terms become graph entities")

    GLOSSARY = (
        "# Glossary\n\n"
        f"**Widget cache {RUN}** — a cache of widgets. Used by the widget service.\n\n"
        f"**Thing ledger {RUN}** — the authoritative record of things.\n\n"
        "Some prose that defines nothing and must not become an entity.\n"
    )

    parsed = entities.parse_glossary(GLOSSARY, {})
    # NOT `names` — that is the module-level helper this file uses in section 1,
    # and shadowing it makes the whole function fail on an earlier line.
    term_names = {p["name"] for p in parsed}
    check("bold-term definitions are parsed", len(parsed) == 2, str(sorted(term_names)))
    check("prose that defines nothing is not an entity",
          not any("must not become" in p["name"] for p in parsed))
    check("the default kind is `concept`, not a technology",
          all(p["kind"] == "concept" for p in parsed),
          str({p["kind"] for p in parsed}))
    check("the definition is captured with the term",
          any("cache of widgets" in p["definition"] for p in parsed))

    # Frontmatter is the more deliberate statement and wins.
    meta = {"entities": [{"name": f"Widget cache {RUN}", "kind": "technology",
                          "aliases": ["widgetcache"]}]}
    over = {p["name"]: p for p in entities.parse_glossary(GLOSSARY, meta)}
    check("frontmatter overrides the parsed kind",
          over[f"Widget cache {RUN}"]["kind"] == "technology")
    check("frontmatter supplies aliases prose cannot",
          "widgetcache" in over[f"Widget cache {RUN}"]["aliases"])

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        report = entities.upsert_glossary_entities(
            c, tenant_id=TENANT, project_id=PROJECT, body=GLOSSARY,
            metadata=meta, source_version="abc123")
        rows = c.execute(text(
            "SELECT canonical_name, kind, tier::text AS tier, attributes "
            "  FROM mem.entities "
            " WHERE tenant_id = :t AND project_id = :p "
            "   AND attributes ->> 'source' = 'glossary' "
            "   AND canonical_name LIKE :like"),
            {"t": str(TENANT), "p": str(PROJECT), "like": f"%{RUN}"}).mappings().all()
    check("glossary terms are written as entities", report["entities"] == 2,
          str(report))
    by_name = {r["canonical_name"]: r for r in rows}
    check("both terms exist in the graph", len(by_name) == 2, str(list(by_name)))

    # Authoritative BY CONSTRUCTION: authored in git, reviewed in a diff,
    # ingested with a commit sha. No proposal queue, because ADR-0002 already
    # counts that as review.
    check("authored entities are authoritative, not proposed",
          all(r["tier"] == "authoritative" for r in rows),
          str({r["tier"] for r in rows}))
    check("provenance records the commit",
          any((r["attributes"] or {}).get("source_version") == "abc123" for r in rows))

    # The point of the whole exercise: the terms must now MATCH in text.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        entities.load_authored(c, tenant_id=TENANT, project_id=PROJECT, refresh=True)
    sentence = f"The Widget cache {RUN} writes into the Thing ledger {RUN} nightly."
    plain = {n for n, _, _ in entities.extract(sentence)}
    scoped = {n for n, _, _ in entities.extract(
        sentence, scope=(str(TENANT), str(PROJECT)))}
    check("authored terms are invisible without scope", not (plain & term_names),
          str(sorted(plain)))
    check("authored terms match once the scope is supplied",
          term_names <= scoped, str(sorted(scoped)))

    # Re-ingesting an unchanged glossary must not multiply rows: ingestion polls.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        entities.upsert_glossary_entities(
            c, tenant_id=TENANT, project_id=PROJECT, body=GLOSSARY,
            metadata=meta, source_version="abc123")
        again = c.execute(text(
            "SELECT count(*) FROM mem.entities "
            " WHERE tenant_id = :t AND project_id = :p "
            "   AND canonical_name LIKE :like"),
            {"t": str(TENANT), "p": str(PROJECT), "like": f"%{RUN}"}).scalar_one()
    check("re-ingesting is idempotent", again == 2, f"{again} rows")

    # ---- self-edges ------------------------------------------------------
    # `RRF uses RRF` and `RLS uses RLS` were in the accepted relationships table.
    # A self-edge is never information, and it inflates an entity's apparent
    # connectivity in the graph arm, which expands seed -> neighbours.
    # ---- the ontology is 14 types; extraction produced exactly one --------
    #
    # Every one of 51 stored proposals was `uses`. Not because prose only
    # expresses that relation, but because the matcher took the FIRST pattern to
    # match and `uses` sat second with a broad alternation (`using|via|through`).
    # "fixed by X connecting through Y" matched `uses` on "through" and stopped
    # before reaching `solved_by`. These cases pin the ordering.
    # ---- domain kinds, not just installed software -----------------------
    #
    # Every entity kind before this was something an engineer INSTALLS —
    # technology, module, system, service. Nothing was something the business
    # MEANS, and that gap is the measured cause of the eval's worst cases: a
    # question in domain language cannot reach an answer in technical language
    # because no node connects them.
    print("\n13. Domain entity kinds")
    KINDED = (
        "## Concepts\n"
        f"**Project isolation {RUN}** — one project never reads another's memories.\n"
        "## Incidents\n"
        f"**Pooler outage {RUN}** — the pooler refused every connection.\n"
        "## People\n"
        f"**Alex {RUN}** — maintainer.\n"
        "## Environments\n"
        f"**staging {RUN}** — the pre-production deployment.\n"
        "## Some unknown heading\n"
        f"**Loose term {RUN}** — no matching kind.\n"
    )
    kinds = {t["name"].split()[0]: t["kind"] for t in entities.parse_glossary(KINDED, {})}
    check("a `Concepts` heading yields `concept`", kinds.get("Project") == "concept",
          str(kinds))
    check("an `Incidents` heading yields `incident`", kinds.get("Pooler") == "incident")
    # "people" is not "persons"; the mapping is explicit rather than derived.
    check("a `People` heading yields `person` (irregular plural)",
          kinds.get("Alex") == "person", str(kinds.get("Alex")))
    check("an `Environments` heading yields `environment`",
          kinds.get("staging") == "environment")
    check("an unknown heading falls back rather than inventing a kind",
          kinds.get("Loose") == entities.DEFAULT_GLOSSARY_KIND, str(kinds.get("Loose")))
    check("the causal/ownership kinds exist",
          {"incident", "person", "environment", "requirement"} <= set(entities.ENTITY_KINDS))

    print("\n12. Relations other than `uses` are reachable")

    def rel(sentence):
        return {r for _, r, _ in entities.extract_relations(sentence)}

    check("contradicts", "contradicts" in rel(
        "We chose PostgreSQL instead of Redis for the cache."))
    check("depends_on", "depends_on" in rel(
        "PgBouncer depends on PostgreSQL for authentication."))
    check("mitigates", "mitigates" in rel(
        "RLS mitigates cross-tenant leakage in PostgreSQL."))
    check("supersedes", "supersedes" in rel(
        "PostgreSQL supersedes Redis as the queue backend."))
    check("documented_in", "documented_in" in rel(
        "The api is documented in the runbook alongside PostgreSQL."))
    check("uses still works as the catch-all", "uses" in rel(
        "The worker reaches PostgreSQL via PgBouncer."))

    # The specific relation must win over the broad one in the same clause.
    both = rel("PgBouncer depends on PostgreSQL running through Docker.")
    check("a specific relation pre-empts `uses` in the same clause",
          "depends_on" in both and "uses" not in both, str(both))

    check("`uses` is ordered last so it cannot pre-empt anything",
          entities.RELATION_PATTERNS[-1][0] == "uses",
          entities.RELATION_PATTERNS[-1][0])

    print("\n11. A self-edge can never be created")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        eid = c.execute(text(
            "SELECT id FROM mem.entities WHERE tenant_id = :t AND project_id = :p "
            " LIMIT 1"), {"t": str(TENANT), "p": str(PROJECT)}).scalar_one()
        try:
            c.execute(text(
                "INSERT INTO mem.relationships "
                "  (tenant_id, project_id, source_id, target_id, relation, tier) "
                "VALUES (:t, :p, :e, :e, 'uses', 'observed')"),
                {"t": str(TENANT), "p": str(PROJECT), "e": str(eid)})
            check("the database refuses a self-edge", False, "INSERT SUCCEEDED")
        except Exception as exc:  # noqa: BLE001
            check("the database refuses a self-edge",
                  "no_self_edge" in str(exc), type(exc).__name__)

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
