"""The identifier (trigram) retrieval arm — ADR-0008's "not optional" arm.

    trigram / identifier search (pg_trgm) for exact codes, file paths, symbols
    and version strings — not optional for this product

It was dead. eval/RESULTS.md recorded it contributing 0% of expected hits across
every case, and nothing failed, because NO TEST EXERCISED IT: tests for the
hybrid path covered the lexical arm only. An arm can return nothing forever if
the only thing watching is a suite that never asks it a question.

The cause was one operator. `identifiers % query` compares WHOLE STRINGS, and
trigram similarity normalises over the union of both, so a memory listing many
identifiers is judged as a whole against a short query and scores badly FOR
HAVING BEEN WELL INDEXED. Measured: conventions.md contains `memory_app
PgBouncer` character for character and scored 0.1927 against it — below the 0.3
threshold, so the arm matched nothing.

The sharpest test here is `adding identifiers must not reduce findability`. That
is the inversion stated directly: it fails against `similarity` and passes
against `word_similarity`, and it would catch any future change that reintroduces
a symmetric whole-string comparison.

    docker compose exec -T api python - < tests/test_identifier_arm.py
"""
from __future__ import annotations

import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, memories  # noqa: E402

RUN = uuid.uuid4().hex[:6]
TENANT = UUID("1de00000-0000-0000-0000-0000000000a1")
PRINCIPAL = UUID("1de00000-0000-0000-0000-0000000000a3")
PROJECT = uuid.uuid5(uuid.NAMESPACE_URL, f"memory-platform:identarm:{RUN}")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) "
                       "VALUES (:i,'identarm','Identifier arm') "
                       "ON CONFLICT DO NOTHING"), {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,:s,'Identifier arm') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT), "s": f"identarm-{RUN}"})
        c.execute(text("INSERT INTO mem.principals "
                       "  (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'human',:e,'identarm') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"identarm-{PRINCIPAL}"})


def write(conn, title: str, content: str) -> UUID:
    return UUID(str(memories.write_memory(
        conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
        mtype="convention", title=title, content=content, source_type="human",
        memory_key=f"identarm:{RUN}:{uuid.uuid4().hex[:8]}")["id"]))


def main() -> None:
    seed()
    print("identifier retrieval arm\n" + "=" * 66)

    # ------------------------------------------------ the operator, in SQL
    # Asserted at the database level because this is where the defect lived, and
    # because the numbers make the inversion undeniable.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        many = ("db.scoped tenant_id principal_id project_id memory_owner "
                "POSTGRES_USER RLS FORCE memory_app PgBouncer SQL "
                "exec_driver_sql mem.ranking_profiles mem.retrieval_events")
        sim, word = c.execute(text(
            "SELECT similarity(:doc, :q), word_similarity(:q, :doc)"),
            {"doc": many, "q": "memory_app PgBouncer"}).one()
    check("whole-string similarity misses a verbatim match",
          float(sim) < 0.3, f"similarity={float(sim):.4f} (threshold 0.3)")
    check("word_similarity finds it",
          float(word) > 0.9, f"word_similarity={float(word):.4f}")

    # ---------------------------------------------------- the arm in practice
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        target = write(
            c, "Database access conventions",
            "Open every transaction with db.scoped(tenant_id, principal_id, "
            "project_id). Never test isolation as memory_owner; isolation tests "
            "connect as memory_app through PgBouncer, because that is the path "
            f"production uses. Fixture {RUN}.")
        write(c, "Unrelated note",
              f"The nightly deploy finished with no errors reported. Run {RUN}.")

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, "memory_app PgBouncer", limit=10,
                               tenant_id=TENANT, project_id=PROJECT)
    by_id = {str(h["id"]): h for h in hits}
    hit = by_id.get(str(target))
    check("an identifier query returns the memory that names them",
          hit is not None, f"{len(hits)} hits")
    check("and the IDENTIFIER arm is what found it",
          bool(hit and hit.get("r_ident")),
          f"r_ident={hit.get('r_ident') if hit else None}")

    # ------------------------------------------- the inversion, stated directly
    #
    # Two memories mention the same identifier. One also lists many others. The
    # richer document must not become HARDER to find because of them — that is
    # the whole defect, and it is what `similarity` gets backwards.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        sparse = write(c, f"Sparse note {RUN}",
                       f"We use exec_driver_sql carefully here. Note {RUN}.")
        # Identifier density matched to a REAL document. An earlier version of
        # this fixture listed a dozen identifiers and passed against the broken
        # operator, because a short identifier string still clears the 0.3
        # similarity threshold — the test agreed with the code and both were
        # wrong. mem.memories.identifiers on conventions.md is ~200 characters,
        # and that is where whole-string similarity collapses.
        rich = write(
            c, f"Rich note {RUN}",
            "Conventions: db.scoped, tenant_id, principal_id, project_id, "
            "memory_owner, POSTGRES_USER, RLS, FORCE, memory_app, PgBouncer, "
            "exec_driver_sql, mem.ranking_profiles, mem.retrieval_events, "
            "rrf_score, alembic_version, search_hybrid, halfvec, hnsw_iterative, "
            "memory_versions_id_seq, fn_set_scope, current_tenant, "
            "allowed_projects, sensitivity_allowed, proposed_relationships, "
            "entity_mentions, memory_supersessions, consolidation_runs, "
            "mcp_tasks, scope_grants, retrieval_events, ranking_profiles, "
            "evaluation_runs, curation_metrics, memory_embeddings, "
            "digest_embedding, importance_prior, quarantine_tier_consistency, "
            f"memories_temporal_uniq, content_hash. Note {RUN}.")

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, "exec_driver_sql", limit=10,
                               tenant_id=TENANT, project_id=PROJECT)
    # Presence in the fused result is NOT the assertion. The vector and lexical
    # arms find these documents anyway, so `id in ids` passed against the broken
    # operator and proved nothing. What must hold is that the IDENTIFIER arm
    # found them — r_ident is the arm's own rank, and None means it did not fire.
    by = {str(h["id"]): h for h in hits}
    check("a sparsely-indexed memory is found BY THE IDENTIFIER ARM",
          bool(by.get(str(sparse), {}).get("r_ident")),
          f"r_ident={by.get(str(sparse), {}).get('r_ident')}")
    check("adding identifiers does not reduce findability",
          bool(by.get(str(rich), {}).get("r_ident")),
          f"r_ident={by.get(str(rich), {}).get('r_ident')} — the richer memory "
          "must not be lost by the arm for being well indexed")

    ident_hits = [h for h in hits if h.get("r_ident")]
    check("the identifier arm contributes to the fused ranking",
          len(ident_hits) > 0, f"{len(ident_hits)} of {len(hits)} via r_ident")

    # ------------------------------------------------ shapes a developer pastes
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        pathy = write(
            c, f"Migration procedure {RUN}",
            "Create migrations/versions/NNNN_short_name.py with down_revision "
            "pointing at the current head. Confirm with SELECT version_num FROM "
            f"public.alembic_version. Ref {RUN}.")
    for query, label in [("alembic_version", "a symbol"),
                         ("down_revision", "an argument name")]:
        with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
            hits = memories.search(c, query, limit=10, tenant_id=TENANT,
                                   project_id=PROJECT)
        found = any(str(h["id"]) == str(pathy) and h.get("r_ident") for h in hits)
        check(f"{label} query reaches the arm ({query})", found,
              f"{len(hits)} hits")

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*66}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
