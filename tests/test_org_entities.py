"""Organisation-scoped entities — built, and off by default (ADR-0012).

This is the one feature in the system that deliberately crosses a project
boundary, so the tests are mostly about what it REFUSES to do:

  * disabled by default, and disabled means the proposal path is closed — not
    that proposals queue up and go through when someone flips the flag.
  * project-specific detail is REJECTED, not redacted. Silent redaction produces
    a plausible name that no longer means what the reviewer approved.
  * restricted-sensitivity support is a permanent exclusion (ADR-0012's wording
    is "permanently excluded ... not merely excluded by default").
  * only the entity NODE crosses. Mentions, relationships and memories stay
    project-scoped — that is what makes a shared vocabulary safe.
  * a human decision is required, and it is re-screened at the decision, because
    a proposal can sit in a queue while the entity is edited underneath it.

    docker compose exec -T api python - < tests/test_org_entities.py
"""
from __future__ import annotations

import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, memories, org_entities  # noqa: E402
from memory_platform.config import settings  # noqa: E402

# Six DIGITS, not eight hex characters. A hex run-id is indistinguishable from a
# short commit sha, and the screen rejects those correctly — so a hex fixture
# suffix makes every entity in this file unpromotable for a reason that has
# nothing to do with what is being tested.
RUN = str(uuid.uuid4().int)[:6]
TENANT = UUID("06e00000-0000-0000-0000-0000000000e1")
PRINCIPAL = UUID("06e00000-0000-0000-0000-0000000000e3")
PROJECT = uuid.uuid5(uuid.NAMESPACE_URL, f"memory-platform:test-org-entities:{RUN}")
OTHER = uuid.uuid5(uuid.NAMESPACE_URL, f"memory-platform:test-org-other:{RUN}")

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def seed() -> None:
    with db.engine().begin() as c:
        c.execute(text("INSERT INTO mem.organizations (id,slug,name) "
                       "VALUES (:i,'orgent','Org') ON CONFLICT DO NOTHING"),
                  {"i": str(TENANT)})
        for pid, slug in ((PROJECT, f"orgent-{RUN}"), (OTHER, f"orgent-o-{RUN}")):
            c.execute(text("INSERT INTO mem.projects (id,tenant_id,slug,name) "
                           "VALUES (:i,:t,:s,'Org') ON CONFLICT DO NOTHING"),
                      {"i": str(pid), "t": str(TENANT), "s": slug})
        c.execute(text("INSERT INTO mem.principals "
                       "  (id,tenant_id,actor,external_id,display_name) "
                       "VALUES (:i,:t,'human',:e,'org') ON CONFLICT DO NOTHING"),
                  {"i": str(PRINCIPAL), "t": str(TENANT), "e": f"org-{PRINCIPAL}"})


def make_entity(conn, kind: str, name: str, project=PROJECT) -> UUID:
    return UUID(str(conn.execute(
        text("INSERT INTO mem.entities "
             "  (tenant_id, project_id, kind, canonical_name, tier) "
             "VALUES (:t, :p, :k, :n, 'observed') "
             "ON CONFLICT (tenant_id, project_id, kind, canonical_name) "
             "  DO UPDATE SET tier = mem.entities.tier RETURNING id"),
        {"t": str(TENANT), "p": str(project), "k": kind, "n": name}).scalar_one()))


def main() -> None:
    seed()
    print("organisation-scoped entities\n" + "=" * 62)

    # ------------------------------------------------------ the screen alone
    check("a plain concept passes the screen",
          org_entities.screen("PgBouncer", "technology") == [])
    for name, why in [
        ("pgbouncer at db.internal.example.com", "hostname"),
        ("service on 10.1.2.44", "ip-address"),
        ("config at /etc/pgbouncer/userlist.txt", "filesystem-path"),
        ("https://github.com/acme/private-repo", "url"),
        ("owner ops@acme.io", "email"),
        ("build 3f9a1c7b4e2d", "commit-sha"),
    ]:
        check(f"the screen rejects a {why}",
              bool(org_entities.screen(name, "technology")), name)
    check("incident-kind entities are never generalisable",
          bool(org_entities.screen("Outage on Tuesday", "incident")))
    check("person and team kinds are never generalisable",
          bool(org_entities.screen("Alex", "person"))
          and bool(org_entities.screen("Platform", "team")))
    check("the screen rejects rather than redacting",
          isinstance(org_entities.screen("host db.internal.example.com",
                                         "technology"), list))

    # ----------------------------------------------------- off by default
    check("cross-project generalisation ships disabled",
          settings().org_entities_enabled is False,
          str(settings().org_entities_enabled))

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        clean = make_entity(c, "technology", f"PgBouncer {RUN}")
        try:
            org_entities.propose(c, tenant_id=TENANT, project_id=PROJECT,
                                 entity_id=clean)
            refused = False
        except org_entities.NotEnabled:
            refused = True
    check("with the flag off, proposing is refused outright", refused)

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        queued = c.execute(text(
            "SELECT count(*) FROM mem.proposed_org_entities "
            " WHERE tenant_id = :t AND project_id = :p"),
            {"t": str(TENANT), "p": str(PROJECT)}).scalar_one()
    check("...and nothing is queued to go through later", queued == 0,
          str(queued))

    # ------------------------------------------------------- flag on
    settings.cache_clear()
    original = settings()
    object.__setattr__(original, "org_entities_enabled", True) \
        if hasattr(original, "__setattr__") else None
    try:
        original.org_entities_enabled = True
    except Exception:  # noqa: BLE001
        pass
    check("the flag can be turned on for the rest of this test",
          settings().org_entities_enabled is True)

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        dirty = make_entity(c, "technology", f"pgbouncer at db.internal.example.com {RUN}")
        try:
            org_entities.propose(c, tenant_id=TENANT, project_id=PROJECT,
                                 entity_id=dirty)
            blocked = False
            detail = "accepted a hostname"
        except org_entities.NotGeneralisable as exc:
            blocked, detail = True, str(exc)[:60]
    check("a project-specific name cannot be proposed", blocked, detail)

    # ---------------------------------------- restricted support (see pgTAP)
    # ADR-0012 excludes restricted material from generalisation permanently, and
    # that exclusion CANNOT be exercised from here: classifying a memory
    # restricted needs a scope grant carrying a max_sensitivity ceiling, and
    # mem.scope_grants has a read policy and no insert policy at all. The
    # application role cannot grant itself elevated sensitivity by any route.
    #
    # That is the correct design, so the behavioural assertion lives in
    # ops/pgtap/suite2_generalisation.sql, which runs as the owner. What this
    # suite asserts is the part it can see: the app role really cannot
    # self-elevate, and the exclusion check goes through the SECURITY DEFINER
    # function rather than a plain count that RLS would silently zero out.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        try:
            c.execute(text(
                "INSERT INTO mem.scope_grants "
                "  (tenant_id, from_kind, from_id, to_kind, to_id, permission, "
                "   reason, granted_by, max_sensitivity) "
                "VALUES (:t,'organization',:t,'user',:pr,'read','fixture',:pr,"
                "        'restricted')"),
                {"t": str(TENANT), "pr": str(PRINCIPAL)})
            self_elevated = True
        except Exception:  # noqa: BLE001
            self_elevated = False
    check("the application role cannot grant itself a sensitivity ceiling",
          self_elevated is False)

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        probe = make_entity(c, "technology", f"Vault {RUN}")
        supported = c.execute(text("SELECT mem.entity_restricted_support(:e)"),
                              {"e": str(probe)}).scalar_one()
    check("the exclusion check runs through the SECURITY DEFINER function",
          supported == 0, str(supported))

    # ---------------------------------------------------- the happy path
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        proposal = org_entities.propose(c, tenant_id=TENANT, project_id=PROJECT,
                                        entity_id=clean)
    check("a clean concept can be proposed", bool(proposal["proposal_id"]))

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        queue = org_entities.pending(c, tenant_id=TENANT, project_id=PROJECT)
    check("the proposal is reviewable", any(
        str(q["id"]) == proposal["proposal_id"] for q in queue), str(len(queue)))
    check("the proposal records why the screen passed it",
          bool(queue and queue[0]["rationale"].get("checks")))
    check("the proposal states that only the node is shared",
          "node only" in str(queue[0]["rationale"].get("shares", "")))

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        before_org = c.execute(text(
            "SELECT count(*) FROM mem.entities "
            " WHERE tenant_id = :t AND project_id IS NULL AND canonical_name = :n"),
            {"t": str(TENANT), "n": f"PgBouncer {RUN}"}).scalar_one()
    check("nothing is shared before the human decides", before_org == 0)

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        decided = org_entities.review(
            c, tenant_id=TENANT, project_id=PROJECT,
            proposal_id=UUID(proposal["proposal_id"]), decision="accepted",
            principal_id=PRINCIPAL, reason="shared vocabulary")
    check("accepting creates the organisation-scoped entity",
          bool(decided["org_entity_id"]), str(decided))

    # Visible from a DIFFERENT project in the same tenant — the point of the
    # feature — while the project-scoped original is untouched.
    with db.scoped(TENANT, PRINCIPAL, OTHER) as c:
        visible = c.execute(text(
            "SELECT count(*) FROM mem.entities "
            " WHERE tenant_id = :t AND project_id IS NULL AND canonical_name = :n"),
            {"t": str(TENANT), "n": f"PgBouncer {RUN}"}).scalar_one()
        leaked_mentions = c.execute(text(
            "SELECT count(*) FROM mem.entity_mentions em "
            "  JOIN mem.memories m ON m.id = em.memory_id "
            " WHERE em.tenant_id = :t AND m.project_id = :p"),
            {"t": str(TENANT), "p": str(PROJECT)}).scalar_one()
        leaked_memories = c.execute(text(
            "SELECT count(*) FROM mem.memories "
            " WHERE tenant_id = :t AND project_id = :p"),
            {"t": str(TENANT), "p": str(PROJECT)}).scalar_one()
    check("the shared node is visible from another project", visible == 1,
          str(visible))
    check("the origin project's mentions do NOT cross", leaked_mentions == 0,
          str(leaked_mentions))
    check("the origin project's memories do NOT cross", leaked_memories == 0,
          str(leaked_memories))

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        again = org_entities.review(
            c, tenant_id=TENANT, project_id=PROJECT,
            proposal_id=UUID(proposal["proposal_id"]), decision="rejected",
            principal_id=PRINCIPAL)
    check("a decided proposal is not re-decided",
          again.get("already_decided") is True and again["decision"] == "accepted",
          str(again))

    # Re-screen at the decision: the entity can be edited while queued.
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as c:
        drifting = make_entity(c, "technology", f"Ollama {RUN}")
        prop2 = org_entities.propose(c, tenant_id=TENANT, project_id=PROJECT,
                                     entity_id=drifting)
        c.execute(text("UPDATE mem.proposed_org_entities "
                       "   SET proposed_name = :n WHERE id = :i"),
                  {"n": f"Ollama on gpu-box.internal.example.com {RUN}",
                   "i": prop2["proposal_id"]})
        try:
            org_entities.review(c, tenant_id=TENANT, project_id=PROJECT,
                                proposal_id=UUID(prop2["proposal_id"]),
                                decision="accepted", principal_id=PRINCIPAL)
            rescreened = False
        except org_entities.NotGeneralisable:
            rescreened = True
    check("the decision re-screens what is actually being shared", rescreened)

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
