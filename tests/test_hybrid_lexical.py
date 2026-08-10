"""Regression coverage for the healthy hybrid lexical fallback.

The vector service can be healthy while strict websearch matching has no row:
one unmatched term makes its default all-terms query empty. In that condition
``mem.search_hybrid`` must still contribute its bounded OR-term lexical arm;
otherwise an incidental vector score is the only path to a known convention.

    docker compose exec -T api python - < tests/test_hybrid_lexical.py
"""
from __future__ import annotations

import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, memories  # noqa: E402


RUN = uuid.uuid4().hex[:8]
TENANT = UUID(f"4b1d{RUN[:4]}-0000-4000-8000-000000000001")
PROJECT = UUID(f"4b1d{RUN[:4]}-0000-4000-8000-000000000002")
PRINCIPAL = UUID(f"4b1d{RUN[:4]}-0000-4000-8000-000000000003")
QUERY = "db.scoped fn_set_scope absent_lexical_term"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO mem.organizations (id, slug, name) VALUES (:id, :slug, 'Hybrid') "
            "ON CONFLICT DO NOTHING"),
            {"id": str(TENANT), "slug": f"hybrid-{RUN}"})
        conn.execute(text(
            "INSERT INTO mem.projects (id, tenant_id, slug, name) "
            "VALUES (:id, :tenant, :slug, 'Hybrid fixture') ON CONFLICT DO NOTHING"),
            {"id": str(PROJECT), "tenant": str(TENANT), "slug": f"hybrid-{RUN}"})
        conn.execute(text(
            "INSERT INTO mem.principals (id, tenant_id, actor, external_id, display_name) "
            "VALUES (:id, :tenant, 'service', :external, 'Hybrid fixture') ON CONFLICT DO NOTHING"),
            {"id": str(PRINCIPAL), "tenant": str(TENANT), "external": f"hybrid-{RUN}"})


def main() -> None:
    seed()
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        target = memories.write_memory(
            conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="convention", title="Open transactions through db.scoped",
            content=("Database access must use db.scoped and fn_set_scope in the "
                     f"same transaction. Hybrid fixture {RUN}."),
            source_type="git", memory_key=f"hybrid-lexical-{RUN}")
        strict = conn.execute(text(
            "SELECT count(*) FROM mem.memories "
            " WHERE content_tsv @@ websearch_to_tsquery('english', :query)"),
            {"query": QUERY}).scalar_one()
        hits = memories.search(conn, QUERY, limit=5, tenant_id=TENANT,
                               project_id=PROJECT)

    target_hit = next((hit for hit in hits if str(hit["id"]) == str(target["id"])), None)
    check("the strict all-terms lexical query has no candidate", strict == 0, str(strict))
    check("the relaxed lexical arm returns the matching convention",
          target_hit is not None and target_hit.get("r_lex") is not None,
          str(target_hit and target_hit.get("r_lex")))
    check("healthy hybrid results are not reported as degraded",
          target_hit is not None and target_hit.get("degraded") is False,
          str(target_hit and target_hit.get("degraded")))

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
