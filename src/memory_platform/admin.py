"""Owner-side administration — operations the application role must not have.

    docker compose run --rm init python -m memory_platform.admin apply-indexes
    docker compose run --rm init python -m memory_platform.admin status

WHY A SEPARATE ENTRYPOINT. `memory_app` deliberately holds no DDL rights: the
role the API runs as must not be able to alter the schema that isolates it.
Automating index creation by granting it CREATE would trade a real security
boundary for convenience.

So the DDL runs where owner credentials already legitimately live — the `init`
container, which is the same place migrations run. Nothing new is trusted; the
work simply happens on the side of the boundary that can do it, on demand
instead of on someone remembering.

`maintenance.index_advice` decides WHETHER an index is warranted (under RLS, as
the app sees it). This applies what that advice says. The split keeps the
decision measurable from inside the application and the privilege outside it.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

from sqlalchemy import create_engine, text

from .config import settings


def _owner_engine():
    """Connect as the migration/owner role, never as memory_app."""
    url = os.environ.get("MEMORY_DATABASE_URL_MIGRATE")
    if not url:
        raise SystemExit(
            "MEMORY_DATABASE_URL_MIGRATE is not set. This command must run where "
            "owner credentials exist — the `init` service:\n"
            "  docker compose run --rm init python -m memory_platform.admin ...")
    return create_engine(url, pool_pre_ping=True)


def _tenants(conn) -> list[tuple[str, str, int]]:
    """(tenant_id, slug, embedding_rows). Runs as owner, so RLS does not apply —
    which is exactly why this cannot live in the API."""
    return [
        (str(r[0]), r[1], int(r[2]))
        for r in conn.execute(text(
            "SELECT o.id, o.slug, count(e.memory_id) "
            "  FROM mem.organizations o "
            "  LEFT JOIN mem.memory_embeddings e ON e.tenant_id = o.id "
            " GROUP BY o.id, o.slug ORDER BY 3 DESC")).all()
    ]


def _index_name(slug: str, tenant_id: str) -> str:
    safe = re.sub(r"[^a-z0-9_]", "_", (slug or tenant_id).lower())[:40]
    return "idx_emb_hnsw_t_" + safe


def apply_indexes(dry_run: bool = False, threshold: int | None = None) -> int:
    """Create partial HNSW indexes for every tenant that has earned one."""
    thr = threshold if threshold is not None else settings().partial_index_threshold
    if thr <= 0:
        print("partial indexes disabled (MEMORY_PARTIAL_INDEX_THRESHOLD=0)")
        return 0

    engine = _owner_engine()
    created = skipped = 0
    with engine.connect() as conn:
        rows = _tenants(conn)

    for tenant_id, slug, n in rows:
        name = _index_name(slug, tenant_id)
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            exists = conn.execute(text(
                "SELECT 1 FROM pg_indexes WHERE schemaname='mem' AND indexname=:n"),
                {"n": name}).scalar_one_or_none()
            if exists:
                print(f"  = {slug:24} {n:>8} rows  index already present")
                skipped += 1
                continue
            if n < thr:
                print(f"  . {slug:24} {n:>8} rows  below threshold {thr}")
                skipped += 1
                continue
            if dry_run:
                print(f"  + {slug:24} {n:>8} rows  WOULD CREATE {name}")
                created += 1
                continue

            print(f"  + {slug:24} {n:>8} rows  building {name} ...", flush=True)
            # CONCURRENTLY: a plain CREATE INDEX takes ACCESS EXCLUSIVE and
            # blocks every write to the embeddings table for the whole build.
            # It cannot run inside a transaction, hence AUTOCOMMIT above.
            conn.execute(text(
                f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} "
                "  ON mem.memory_embeddings USING hnsw (embedding halfvec_cosine_ops) "
                f" WHERE tenant_id = '{tenant_id}'"))
            print(f"    done: {name}")
            created += 1

    print(f"\n{created} created, {skipped} skipped (threshold {thr})")
    return 0


def status() -> int:
    engine = _owner_engine()
    with engine.connect() as conn:
        rows = _tenants(conn)
        idx = {r[0] for r in conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname='mem' "
            "   AND indexname LIKE 'idx_emb_hnsw_t_%'")).all()}
    thr = settings().partial_index_threshold
    print(f"threshold: {thr}\n")
    print(f"  {'tenant':24} {'vectors':>8}  partial index")
    for tenant_id, slug, n in rows:
        name = _index_name(slug, tenant_id)
        state = "present" if name in idx else ("ADVISED" if n >= thr > 0 else "-")
        print(f"  {slug:24} {n:>8}  {state}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="memory_platform.admin",
                                description="Owner-side maintenance")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply-indexes", help="create advised partial HNSW indexes")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--threshold", type=int,
                   help="override MEMORY_PARTIAL_INDEX_THRESHOLD for this run")
    a.set_defaults(fn=lambda ns: apply_indexes(ns.dry_run, ns.threshold))

    s = sub.add_parser("status", help="show per-tenant vector counts and indexes")
    s.set_defaults(fn=lambda ns: status())

    ns = p.parse_args(argv)
    return ns.fn(ns)


if __name__ == "__main__":
    sys.exit(main())
