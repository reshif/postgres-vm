"""Write-path tests: tier assignment, idempotency, quarantine, retrieval.

The assertions that matter here are the ones about trust, because they are the
security surface: if source_type can be laundered into a higher tier, the whole
quarantine model in ADR-0015 is decorative.

    docker compose exec -T api python - < tests/test_write_path.py
"""
from __future__ import annotations

import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, memories  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("cccccccc-0000-0000-0000-00000000000c")
PROJECT = UUID("cccccccc-0000-0000-0000-0000000000c1")
PRINCIPAL = UUID("cccccccc-0000-0000-0000-0000000000c2")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) VALUES (:i,'tenant-c','C') "
                       "ON CONFLICT DO NOTHING"), {"i": str(TENANT)})
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,'proj-c','C') ON CONFLICT DO NOTHING"),
                  {"i": str(PROJECT), "t": str(TENANT)})
        c.execute(text("INSERT INTO mem.principals (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'agent',:e,'test') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"ext-c-{PRINCIPAL}"})


def main() -> None:
    seed()

    # ---- 1. pure functions -------------------------------------------------
    print("\n1. Hashing, digest, tokens, identifiers")
    a = memories.content_hash("hello   world\n\n")
    b = memories.content_hash("hello world")
    check("content_hash normalises whitespace", a == b)
    check("content_hash differs on real change",
          memories.content_hash("hello world!") != a)

    long = "First sentence here. " + ("padding words " * 200)
    d = memories.make_digest("t", long)
    check("digest respects the 400-char DB limit", len(d) <= memories.MAX_DIGEST, f"{len(d)}")

    idents = memories.extract_identifiers(
        "Fix db.scoped in memory_platform/db.py", "raised ERR_TIMEOUT in HttpClient")
    found = {"db.scoped", "ERR_TIMEOUT", "HttpClient"} & set(idents.split())
    check("identifiers pick up symbols/paths/constants", len(found) >= 3, idents[:60])

    # ---- 2. tier assignment is source-driven -------------------------------
    print("\n2. Trust tier assignment (server-side only)")
    for src, want in [("git", "authoritative"), ("ci", "verified"),
                      ("commit", "observed"), ("agent", "inferred"),
                      ("who-knows", "untrusted")]:
        got = memories.assign_tier(src)
        check(f"source {src!r} -> {want}", got == want, got)

    # ---- 3. write + quarantine --------------------------------------------
    print("\n3. Writes")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        r = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title="Postgres over a dedicated vector DB",
            content=("We evaluated Qdrant and Weaviate and chose Postgres with pgvector "
                     "for the MVP, because operating one datastore beats operating two "
                     f"until recall or latency proves otherwise. Run {RUN}."),
            source_type="git", memory_key=f"adr-0001-{RUN}",
            source_uri="/.memory/decisions/ADR-0001.md", source_version="abc123",
        )
        check("git-sourced write is authoritative", r["tier"] == "authoritative", r["tier"])
        check("authoritative write is active", r["status"] == "active", r["status"])
        check("write produced an embedding", r["embedded"] is True)
        first_id = r["id"]

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        r2 = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="observation", title="Agent guessed something",
            content=f"An LLM inferred this from a README at {RUN}.",
            source_type="agent", memory_key=f"inf-{RUN}")
        check("agent-sourced write is inferred", r2["tier"] == "inferred", r2["tier"])
        check("inferred write is QUARANTINED", r2["status"] == "quarantined", r2["status"])

    # ---- 4. idempotency ----------------------------------------------------
    print("\n4. Idempotency (re-ingest must not duplicate)")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        again = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title="Postgres over a dedicated vector DB",
            content=("We evaluated Qdrant and Weaviate and chose Postgres with pgvector "
                     "for the MVP, because operating one datastore beats operating two "
                     f"until recall or latency proves otherwise. Run {RUN}."),
            source_type="git", memory_key=f"adr-0001-{RUN}")
        check("re-ingest deduplicates", again["deduplicated"] is True)
        check("re-ingest returns the same id", str(again["id"]) == str(first_id))
        check("dedup path returns the same keys as create",
              {"tier","status","embedded"} <= set(again), sorted(again))

    # ---- 5. retrieval ------------------------------------------------------
    print("\n5. Retrieval")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        hits = memories.search(c, "why did we choose postgres instead of qdrant", limit=5)
        check("search returns the decision", len(hits) >= 1, f"{len(hits)} hits")
        if hits:
            top = hits[0]
            check("top hit is the authoritative decision",
                  "Postgres" in top["title"], top["title"][:40])
            check("search reports non-degraded", top["degraded"] is False)

        quarantined_visible = any("guessed" in h["title"] for h in
                                  memories.search(c, "LLM inferred readme", limit=10))
        check("quarantined memory is NOT returned by search", not quarantined_visible)

    # ---- identical content in two projects of one tenant ------------------
    # Found while building extraction, but not an extraction bug. Uniqueness was
    # tenant-wide (tenant_id, memory_key, valid_at) while VISIBILITY is
    # scope-wide, so the dedup lookup — which runs under RLS — could not see the
    # row the constraint would then reject. The write died with a raw
    # ExclusionViolation naming an object the caller cannot query.
    #
    # Two ordinary situations reach it: two projects in one tenant sharing a
    # convention file, and any tenant where the same `.memory/` path exists in
    # two registered projects (ingest derives memory_key from the path, so the
    # keys collide exactly). Migration 0013 puts scope in the constraint.
    print("\n9. The same content in two projects of one tenant")
    other = uuid.uuid4()
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                       "VALUES (:i,:t,:s,'C2') ON CONFLICT DO NOTHING"),
                  {"i": str(other), "t": str(TENANT), "s": f"proj-c2-{other.hex[:8]}"})

    shared = f"Both projects follow the same release checklist. {RUN}"
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        first = memories.write_memory(
            c, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="convention", title="Release checklist", content=shared,
            source_type="git", memory_key=f"conventions/release-{RUN}.md")
    try:
        with db.scoped(TENANT, PRINCIPAL, other) as c:
            second = memories.write_memory(
                c, tenant_id=TENANT, project_id=other, principal_id=PRINCIPAL,
                mtype="convention", title="Release checklist", content=shared,
                source_type="git", memory_key=f"conventions/release-{RUN}.md")
        check("the second project can write the same content", True)
        check("it is a separate memory, not a shared one",
              str(second["id"]) != str(first["id"]))
    except Exception as exc:  # noqa: BLE001
        check("the second project can write the same content", False,
              type(exc).__name__)
        check("it is a separate memory, not a shared one", False, "not reached")

    # ...and each project still sees only its own.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        n = c.execute(text("SELECT count(*) FROM mem.memories "
                           " WHERE tenant_id = :t AND content_hash = :h"),
                      {"t": str(TENANT),
                       "h": memories.content_hash(shared)}).scalar_one()
    check("each project sees exactly one copy under RLS", n == 1, f"{n} visible")

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*60}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
