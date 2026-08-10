"""The CI test 01-SCHEMA.sql promises but never had.

    -- ... apply to every tenant-scoped table. A CI test asserts none are missed.

None were asserted, and several were missed — including mem.memory_versions,
which the versioning trigger fills with complete copies of every memory row and
which had no RLS at all. Tenant B could read tenant A's memory content out of it
while `SELECT count(*) FROM mem.memories` correctly returned zero.

This test exists so that class of bug cannot recur silently. It is structural,
not example-based: it asks the catalog which tables carry a tenant_id and demands
that each one is protected. A new table added in a later phase is covered the day
it is created, without anyone remembering to extend a list.

It also PROVES the leak is closed by writing a marked secret as one tenant and
reading every tenant-scoped table as another, rather than trusting that the
policies say what they appear to say.

    docker compose exec -T api python - < tests/test_rls_coverage.py
"""
from __future__ import annotations

import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, memories  # noqa: E402

RUN = uuid.uuid4().hex[:8]
A = UUID("eeeeeeee-0000-0000-0000-00000000000e")
PA = UUID("eeeeeeee-0000-0000-0000-0000000000e1")
RA = UUID("eeeeeeee-0000-0000-0000-0000000000e2")
B = UUID("ffffffff-0000-0000-0000-00000000000f")
PB = UUID("ffffffff-0000-0000-0000-0000000000f1")
RB = UUID("ffffffff-0000-0000-0000-0000000000f2")

# Named, reasoned exemptions. Anything not listed here MUST be protected.
# These are registry metadata, not memory content, and project binding must
# resolve a project before a scope exists (05-BUILD-PLAN Phase 2), so putting
# RLS on them requires a privileged resolution path designed alongside it.
# Listed rather than filtered out so they stay visible in every run.
EXEMPT = {
    "projects": "registry metadata; resolved pre-scope during project binding",
    "principals": "registry metadata; resolved pre-scope during auth",
}

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


TENANT_SCOPED = text("""
SELECT c.relname AS t, c.relrowsecurity AS rls, c.relforcerowsecurity AS forced,
       coalesce(string_agg(DISTINCT p.cmd, ','), '') AS cmds
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN information_schema.columns col
    ON col.table_schema = 'mem' AND col.table_name = c.relname
   AND col.column_name = 'tenant_id'
  LEFT JOIN pg_policies p ON p.schemaname = 'mem' AND p.tablename = c.relname
 WHERE n.nspname = 'mem' AND c.relkind = 'r'
 GROUP BY 1, 2, 3
 ORDER BY 1
""")


def seed() -> None:
    with db.engine().begin() as c:
        for t, p, r, s in ((A, PA, RA, "leak-a"), (B, PB, RB, "leak-b")):
            c.execute(text("INSERT INTO mem.organizations (id,slug,name) VALUES (:i,:s,:s) "
                           "ON CONFLICT DO NOTHING"), {"i": str(t), "s": s})
            c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                           "VALUES (:i,:t,:s,:s) ON CONFLICT DO NOTHING"),
                      {"i": str(p), "t": str(t), "s": s})
            c.execute(text("INSERT INTO mem.principals (id,tenant_id,actor,external_id,display_name) "
                           "VALUES (:i,:t,'agent',:e,'x') ON CONFLICT DO NOTHING"),
                      {"i": str(r), "t": str(t), "e": f"e-{r}"})


def main() -> None:
    seed()

    # ---- 1. structural: every tenant-scoped table is protected -------------
    print("\n1. RLS coverage across every table carrying tenant_id")
    with db.engine().connect() as c:
        rows = c.execute(TENANT_SCOPED).mappings().all()

    for r in rows:
        t = r["t"]
        if t in EXEMPT:
            print(f"  SKIP  {t}  (exempt: {EXEMPT[t]})")
            continue
        check(f"{t}: RLS enabled and FORCEd", r["rls"] and r["forced"],
              f"rls={r['rls']} forced={r['forced']}")
        check(f"{t}: has a SELECT policy", "SELECT" in r["cmds"], r["cmds"] or "(none)")

    unknown_exempt = set(EXEMPT) - {r["t"] for r in rows}
    check("exemption list has no stale entries", not unknown_exempt, str(unknown_exempt))

    # ---- 2. behavioural: the leak is actually closed -----------------------
    print("\n2. Cross-tenant read attempt on every protected table")
    secret = f"TENANT-A-CONFIDENTIAL-{RUN} acquisition target is Initech"
    with db.scoped(A, RA, PA) as c:
        memories.write_memory(
            c, tenant_id=A, project_id=PA, principal_id=RA, mtype="decision",
            title=f"Acquisition plan {RUN}", content=secret,
            source_type="git", memory_key=f"secret-{RUN}")

    protected = [r["t"] for r in rows if r["t"] not in EXEMPT]
    leaked: list[str] = []
    with db.scoped(B, RB, PB) as c:
        for t in protected:
            cols = c.execute(text(
                "SELECT string_agg(column_name, ',') FROM information_schema.columns "
                "WHERE table_schema='mem' AND table_name=:t "
                "AND data_type IN ('text','jsonb','character varying')"
            ), {"t": t}).scalar_one_or_none()
            if not cols:
                continue
            expr = " || ' ' || ".join(f"coalesce({c_}::text,'')" for c_ in cols.split(","))
            n = c.execute(text(
                f"SELECT count(*) FROM mem.{t} WHERE ({expr}) LIKE :pat"
            ), {"pat": f"%TENANT-A-CONFIDENTIAL-{RUN}%"}).scalar_one()
            if n:
                leaked.append(f"{t}({n})")
    check("tenant A's secret is invisible in every protected table",
          not leaked, ", ".join(leaked) or "none")

    # ---- 3. the leak that started this -------------------------------------
    print("\n3. Regression: memory_versions specifically")
    with db.scoped(B, RB, PB) as c:
        n = c.execute(text(
            "SELECT count(*) FROM mem.memory_versions "
            "WHERE snapshot->>'content' LIKE :p"
        ), {"p": f"%TENANT-A-CONFIDENTIAL-{RUN}%"}).scalar_one()
        check("memory_versions does not leak snapshots cross-tenant", n == 0, f"saw {n}")

    with db.scoped(A, RA, PA) as c:
        n = c.execute(text(
            "SELECT count(*) FROM mem.memory_versions "
            "WHERE snapshot->>'content' LIKE :p"
        ), {"p": f"%TENANT-A-CONFIDENTIAL-{RUN}%"}).scalar_one()
        check("tenant A can still read its OWN version history", n >= 1, f"saw {n}")

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
