"""Golden-case exports use stable labels and obey the pack's scope."""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from memory_platform import api, context, db, memories

RUN = "eval-export"
TENANT = UUID("a7777777-0000-0000-0000-000000000001")
PROJECT = UUID("a7777777-0000-0000-0000-000000000002")
PRINCIPAL = UUID("a7777777-0000-0000-0000-000000000003")
OTHER_TENANT = UUID("a7777777-0000-0000-0000-000000000004")
OTHER_PROJECT = UUID("a7777777-0000-0000-0000-000000000005")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed_registry() -> None:
    with db.engine().begin() as conn:
        for tenant, slug in ((TENANT, "eval-export"), (OTHER_TENANT, "eval-export-other")):
            conn.execute(text(
                "INSERT INTO mem.organizations (id, slug, name) VALUES (:id, :slug, :slug) "
                "ON CONFLICT DO NOTHING"), {"id": str(tenant), "slug": slug})
        conn.execute(text(
            "INSERT INTO mem.projects (id, tenant_id, slug, name) "
            "VALUES (:id, :tenant, :slug, :slug) ON CONFLICT DO NOTHING"),
            {"id": str(PROJECT), "tenant": str(TENANT), "slug": "eval-export"})
        conn.execute(text(
            "INSERT INTO mem.projects (id, tenant_id, slug, name) "
            "VALUES (:id, :tenant, :slug, :slug) ON CONFLICT DO NOTHING"),
            {"id": str(OTHER_PROJECT), "tenant": str(OTHER_TENANT), "slug": "eval-export-other"})
        conn.execute(text(
            "INSERT INTO mem.principals (id, tenant_id, actor, external_id, display_name) "
            "VALUES (:id, :tenant, 'agent', :external, 'eval export') ON CONFLICT DO NOTHING"),
            {"id": str(PRINCIPAL), "tenant": str(TENANT), "external": f"{RUN}-{PRINCIPAL}"})


def main() -> None:
    seed_registry()
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        memories.write_memory(
            conn, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
            mtype="decision", title="Keep one datastore", source_type="git",
            content="Postgres with pgvector is the single datastore for this project.",
            memory_key="eval-export:single-store",
        )
        pack = context.build_pack(
            conn, "why do we keep one datastore?", tenant_id=TENANT,
            project_id=PROJECT, principal_id=PRINCIPAL,
        )

    template = api.eval_case_template(
        tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL,
        pack_id=pack["pack_id"],
    )
    check("template preserves the real query",
          template["case"]["query"] == "why do we keep one datastore?")
    check("template suggests stable keys and 12-character hashes",
          bool(template["candidates"]) and all(
              item["key"] and len(item["hash"]) == 12 for item in template["candidates"]),
          str(template["candidates"][:1]))
    check("template never marks a returned item as ground truth",
          template["case"]["expect"] == []
          and template["case"]["forbidden_memory_ids"] == [])

    try:
        api.eval_case_template(
            tenant_id=OTHER_TENANT, project_id=OTHER_PROJECT, principal_id=OTHER_TENANT,
            pack_id=pack["pack_id"],
        )
    except api.HTTPException as exc:
        check("another scope cannot export this retrieval event", exc.status_code == 404)
    else:
        check("another scope cannot export this retrieval event", False)

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
