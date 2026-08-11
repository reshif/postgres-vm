"""Isolation and write-path tests — the negative tests Phase 1 requires.

05-BUILD-PLAN.md: "RLS with FORCE from the very first migration. Retrofitting
isolation is a rewrite, and the negative tests must exist before there is data to
leak." This is that suite.

It deliberately exercises the real application path — db.scoped(), the pooled
PgBouncer connection, the memory_app role — rather than raw SQL as the owner.
memory_owner is the image's POSTGRES_USER and therefore a SUPERUSER, which
bypasses RLS entirely including FORCE. A test that passes as memory_owner proves
nothing at all about isolation.

Run inside the api container (it has the deps and the network names):

    docker compose exec -T api python - < tests/test_isolation.py

Re-runnable: each run writes memories under a fresh suffix, so nothing collides
with the temporal unique constraint. Rows accumulate only under the two test
tenants. memory_app holds no DELETE grant by design, so purging is an owner-side
admin operation:

    docker compose exec -T postgres psql -U memory_owner -d memory -c \\
      "DELETE FROM mem.organizations WHERE slug IN ('tenant-a','tenant-b');"
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import uuid as _uuid
from uuid import UUID

# Fresh per run so re-running does not trip the temporal unique constraint on
# (tenant, memory_key) — the test proves isolation, not upsert semantics.
RUN = _uuid.uuid4().hex[:8]

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db  # noqa: E402

# Fixed ids so a re-run is idempotent and cleanup is trivial.
TENANT_A = UUID("aaaaaaaa-0000-0000-0000-00000000000a")
TENANT_B = UUID("bbbbbbbb-0000-0000-0000-00000000000b")
PROJ_A = UUID("aaaaaaaa-0000-0000-0000-0000000000a1")
PROJ_B = UUID("bbbbbbbb-0000-0000-0000-0000000000b1")
PRIN_A = UUID("aaaaaaaa-0000-0000-0000-0000000000a2")
PRIN_B = UUID("bbbbbbbb-0000-0000-0000-0000000000b2")
MODEL = "bge-m3@1"

OLLAMA = os.environ.get("MEMORY_EMBEDDING_URL", "http://host.docker.internal:11434")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def embed(txt: str) -> str:
    """Real embedding via Ollama, returned in pgvector literal form."""
    req = urllib.request.Request(
        f"{OLLAMA}/api/embed",
        data=json.dumps({"model": "bge-m3", "input": txt}).encode(),
        headers={"Content-Type": "application/json"},
    )
    vec = json.load(urllib.request.urlopen(req, timeout=60))["embeddings"][0]
    assert len(vec) == 1024, f"expected 1024 dims, got {len(vec)}"
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def seed() -> None:
    """Tenants/projects/principals carry no RLS, so the app role can seed them."""
    with db.engine().begin() as c:
        for tid, slug in ((TENANT_A, "tenant-a"), (TENANT_B, "tenant-b")):
            c.execute(text("INSERT INTO mem.organizations (id,slug,name) VALUES (:i,:s,:s) "
                           "ON CONFLICT DO NOTHING"), {"i": str(tid), "s": slug})
        for pid, tid, slug in ((PROJ_A, TENANT_A, "proj-a"), (PROJ_B, TENANT_B, "proj-b")):
            c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                           "VALUES (:i,:t,:s,:s) ON CONFLICT DO NOTHING"),
                      {"i": str(pid), "t": str(tid), "s": slug})
        for prid, tid in ((PRIN_A, TENANT_A), (PRIN_B, TENANT_B)):
            c.execute(text("INSERT INTO mem.principals (id,tenant_id,actor,external_id,display_name) "
                           "VALUES (:i,:t,'agent',:e,'test') ON CONFLICT DO NOTHING"),
                      {"i": str(prid), "t": str(tid), "e": f"ext-{prid}"})
        c.execute(text("INSERT INTO mem.embedding_models (id,provider,dimensions,normalized,is_active,is_primary) "
                       "VALUES (:m,'ollama',1024,true,true,true) ON CONFLICT DO NOTHING"), {"m": MODEL})


INSERT_MEMORY = text("""
INSERT INTO mem.memories
  (tenant_id, memory_key, type, title, content, digest, scope_kind, project_id,
   tier, source_type, content_hash)
VALUES
  (:tenant, :key, 'decision', :title, :content, :digest, 'project', :project,
   'verified', 'test', :hash)
RETURNING id
""")


def main() -> None:
    seed()

    # ---- 1. the write path that migration 0003 unblocked -------------------
    print("\n1. Scoped write path (memory + embedding as memory_app)")
    vec = embed("We chose Postgres over a dedicated vector database for the MVP.")
    with db.scoped(TENANT_A, PRIN_A, PROJ_A) as c:
        mid = c.execute(INSERT_MEMORY, {
            "tenant": str(TENANT_A), "key": f"iso-test-{RUN}",
            "title": "Use Postgres for vectors",
            "content": "We chose Postgres over a dedicated vector database for the MVP.",
            "digest": "Postgres chosen over dedicated vector DB.",
            "project": str(PROJ_A), "hash": f"isohash-{RUN}",
        }).scalar_one()
        check("memory INSERT succeeds when scoped", True, str(mid)[:8])
        c.execute(text("INSERT INTO mem.memory_embeddings (memory_id, model_id, tenant_id, embedding) "
                       "VALUES (:m, :mo, :t, CAST(:v AS halfvec(1024)))"),
                  {"m": str(mid), "mo": MODEL, "t": str(TENANT_A), "v": vec})
        check("embedding INSERT succeeds (was blocked before 0003)", True)

    # ---- 2. cross-tenant read ---------------------------------------------
    print("\n2. Cross-tenant isolation")
    with db.scoped(TENANT_B, PRIN_B, PROJ_B) as c:
        n = c.execute(text("SELECT count(*) FROM mem.memories")).scalar_one()
        check("tenant B sees 0 of tenant A's memories", n == 0, f"saw {n}")
        ne = c.execute(text("SELECT count(*) FROM mem.memory_embeddings")).scalar_one()
        check("tenant B sees 0 of tenant A's embeddings", ne == 0, f"saw {ne}")

    with db.scoped(TENANT_A, PRIN_A, PROJ_A) as c:
        n = c.execute(text("SELECT count(*) FROM mem.memories")).scalar_one()
        check("tenant A still sees its own memory", n >= 1, f"saw {n}")

    # ---- 3. unscoped ------------------------------------------------------
    print("\n3. Unscoped access")
    with db.engine().connect() as c:
        n = c.execute(text("SELECT count(*) FROM mem.memories")).scalar_one()
        check("unscoped connection sees 0 rows", n == 0, f"saw {n}")

    # ---- 4. cross-tenant write forgery ------------------------------------
    print("\n4. Write forgery (scope A, claim tenant B)")
    try:
        with db.scoped(TENANT_A, PRIN_A, PROJ_A) as c:
            c.execute(INSERT_MEMORY, {
                "tenant": str(TENANT_B), "key": f"forged-{RUN}", "title": "forged",
                "content": "x", "digest": "x", "project": str(PROJ_B), "hash": f"forged-{RUN}",
            })
        check("forged cross-tenant INSERT is rejected", False, "IT SUCCEEDED")
    except Exception as exc:
        check("forged cross-tenant INSERT is rejected", "row-level security" in str(exc),
              type(exc).__name__)

    # ---- 5. retrieval -----------------------------------------------------
    print("\n5. Hybrid retrieval returns the memory to its own tenant")
    q = embed("why did we pick postgres for storing vectors")
    with db.scoped(TENANT_A, PRIN_A, PROJ_A) as c:
        rows = c.execute(text(
            "SELECT memory_id, rrf_score, r_vec, r_lex FROM mem.search_hybrid("
            "CAST(:v AS halfvec(1024)), :q, NULL, 'observed', now(), NULL, 10, 60)"
        ), {"v": q, "q": "postgres vector database"}).all()
        check("search_hybrid returns >=1 row for tenant A", len(rows) >= 1, f"{len(rows)} rows")
        if rows:
            print(f"      top: score={rows[0][1]:.5f} r_vec={rows[0][2]} r_lex={rows[0][3]}")

    with db.scoped(TENANT_B, PRIN_B, PROJ_B) as c:
        rows = c.execute(text(
            "SELECT memory_id FROM mem.search_hybrid("
            "CAST(:v AS halfvec(1024)), :q, NULL, 'observed', now(), NULL, 10, 60)"
        ), {"v": q, "q": "postgres vector database"}).all()
        check("search_hybrid returns 0 rows for tenant B", len(rows) == 0, f"{len(rows)} rows")

    # ---- 6. sensitivity (Suite 2) -----------------------------------------
    #
    # Two of Suite 2's nine cases had nothing to test: `mem.memories.sensitivity`
    # and `mem.scope_grants` existed in the schema and no code read either, so a
    # `restricted` memory was readable by anyone already inside the project. The
    # column was decoration.
    #
    # Enforcement now lives in the RLS policy, not in Python, for the same reason
    # fn_set_scope exists — a check the application performs is one forgotten
    # join from being skipped, and RLS failures are silent. So these assertions
    # query the TABLE DIRECTLY as memory_app: if raw SQL obeys the rule, every
    # code path above it does too.
    #
    # The grant-visibility ladder (ceiling, expiry) is asserted at the DATABASE
    # layer in ops/pgtap/, because issuing a grant requires privileges the
    # application role deliberately does not have — see the escalation check
    # below. Suite 2 is specified to be implemented twice; that is the seam.
    print("\n6. Sensitivity is enforced by the policy (Suite 2)")

    with db.scoped(TENANT_A, PRIN_A, PROJ_A) as c:
        # Written restricted from the start. Raising an existing row's
        # sensitivity is refused for the writer, because the updated row would be
        # invisible to them — correct, and the reason classification is an
        # owner-side operation.
        c.execute(text(
            "INSERT INTO mem.memories "
            "  (tenant_id, memory_key, type, title, content, digest, scope_kind, "
            "   project_id, tier, source_type, content_hash, sensitivity) "
            "VALUES (:t, :k, 'decision', :ti, :co, :d, 'project', :p, "
            "        'authoritative', 'git', :h, 'restricted')"),
            {"t": str(TENANT_A), "k": f"iso-secret-{RUN}",
             "ti": f"Key rotation policy {RUN}",
             "co": f"The vault master key rotates quarterly. {RUN}",
             "d": "Key rotation policy.", "p": str(PROJ_A),
             "h": f"isosecret-{RUN}"})

    with db.scoped(TENANT_A, PRIN_A, PROJ_A) as c:
        seen = c.execute(text(
            "SELECT count(*) FROM mem.memories WHERE memory_key = :k"),
            {"k": f"iso-secret-{RUN}"}).scalar_one()
    check("a restricted memory is NOT returned without a grant", seen == 0,
          f"visible={seen}")

    with db.scoped(TENANT_A, PRIN_A, PROJ_A) as c:
        ordinary = c.execute(text(
            "SELECT count(*) FROM mem.memories WHERE memory_key = :k"),
            {"k": f"iso-test-{RUN}"}).scalar_one()
    check("an ordinary memory in the same scope is unaffected", ordinary == 1,
          f"visible={ordinary}")

    # The derived copy matters as much as the original: the version trigger
    # copies whole rows, so gating `memories` alone would be a locked door beside
    # an open window — the shape of hole migration 0005 closed for cross-tenant.
    with db.scoped(TENANT_A, PRIN_A, PROJ_A) as c:
        leaked = c.execute(text(
            "SELECT count(*) FROM mem.memory_versions v "
            "  WHERE v.memory_id NOT IN (SELECT id FROM mem.memories)")).scalar_one()
    check("no version row survives whose memory is not readable", leaked == 0,
          f"{leaked} orphan versions visible")

    # PRIVILEGE ESCALATION. The application role must not be able to widen its
    # own access. If memory_app could insert a grant, the sensitivity gate would
    # be a suggestion — anything that can read the API could read everything.
    try:
        with db.scoped(TENANT_A, PRIN_A, PROJ_A) as c:
            c.execute(text(
                "INSERT INTO mem.scope_grants "
                "  (tenant_id, from_kind, from_id, to_kind, to_id, permission, "
                "   reason, granted_by, max_sensitivity) "
                "VALUES (:t,'project',:p,'user',:u,'read','self-grant',:u,'restricted')"),
                {"t": str(TENANT_A), "p": str(PROJ_A), "u": str(PRIN_A)})
        check("the app role CANNOT grant itself access", False, "IT SUCCEEDED")
    except Exception as exc:  # noqa: BLE001
        check("the app role CANNOT grant itself access",
              "row-level security" in str(exc) or "permission denied" in str(exc),
              type(exc).__name__)

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*60}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
