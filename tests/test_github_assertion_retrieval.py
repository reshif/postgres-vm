"""Accepted GitHub assertions are retrievable without importing Git blob text."""
from __future__ import annotations

from datetime import datetime, timezone
import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import api, context, db, evidence_assertions, github_projection  # noqa: E402


RUN = uuid.uuid4().hex[:8]
TENANT = UUID("2f200000-0000-0000-0000-0000000000a1")
PRINCIPAL = UUID("2f200000-0000-0000-0000-0000000000a3")
PROJECT = uuid.uuid5(uuid.NAMESPACE_URL, f"memory-platform:github-retrieval:{RUN}")
SOURCE_REPO = f"https://github.com/github-retrieval-{RUN}/service"
EVIDENCE_REPO = f"https://github.com/github-retrieval-{RUN}/service-evidence"
SOURCE_SHA = "a" * 40
SIDE_SHA = "b" * 40
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def assertion(key: str, *, state: str = "accepted", kind: str = "decision") -> str:
    return f"""---
id: {key}
subject: project storage
predicate: uses
object: PostgreSQL with pgvector for project vectors
type: {kind}
state: {state}
confidence: 0.960
evidence:
  - github://github.com/github-retrieval-{RUN}/service@{SOURCE_SHA}:src/storage.py
---
Review body that must never enter the retrieval corpus.
"""


def seed() -> UUID:
    with db.engine().begin() as conn:
        conn.execute(text("INSERT INTO mem.organizations (id, slug, name) VALUES (:id, :slug, :slug) "
                          "ON CONFLICT (id) DO NOTHING"),
                     {"id": str(TENANT), "slug": f"github-retrieval-{RUN}"})
        conn.execute(text("INSERT INTO mem.projects "
                          "(id, tenant_id, slug, name, repo_url, source_provider, evidence_repo_url, "
                          " github_installation_id, git_default_branch) "
                          "VALUES (:id, :tenant, :slug, :slug, :repo, 'github', :evidence, 42, 'main')"),
                     {"id": str(PROJECT), "tenant": str(TENANT), "repo": SOURCE_REPO,
                      "evidence": EVIDENCE_REPO, "slug": f"service-{RUN}"})
        conn.execute(text("INSERT INTO mem.principals "
                          "(id, tenant_id, actor, external_id, display_name) "
                          "VALUES (:id, :tenant, 'service', :external, 'GitHub retrieval test') "
                          "ON CONFLICT DO NOTHING"),
                     {"id": str(PRINCIPAL), "tenant": str(TENANT), "external": f"github-retrieval-{RUN}"})

    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        support = evidence_assertions.record_git_blob(
            conn, tenant_id=TENANT, project_id=PROJECT, repository_url=SOURCE_REPO,
            revision=SOURCE_SHA, path="src/storage.py", content_sha256="c" * 64,
            byte_size=110, observed_at=datetime.now(timezone.utc))
        sidecar = evidence_assertions.record_git_blob(
            conn, tenant_id=TENANT, project_id=PROJECT, repository_url=EVIDENCE_REPO,
            revision=SIDE_SHA, path="assertions/storage.md", content_sha256="d" * 64,
            byte_size=170, observed_at=datetime.now(timezone.utc))
        accepted = evidence_assertions.parse_assertion("assertions/storage.md", assertion("storage"))
        stored = evidence_assertions.write_assertion(
            conn, tenant_id=TENANT, project_id=PROJECT, document=accepted,
            source_repository=EVIDENCE_REPO, source_path="assertions/storage.md",
            source_revision=SIDE_SHA, source_artifact_id=sidecar,
            supporting_artifact_ids=[support], accepted_by=PRINCIPAL)
        retracted = evidence_assertions.parse_assertion(
            "assertions/withdrawn.md", assertion("withdrawn", state="retracted"))
        sidecar_retracted = evidence_assertions.record_git_blob(
            conn, tenant_id=TENANT, project_id=PROJECT, repository_url=EVIDENCE_REPO,
            revision="e" * 40, path="assertions/withdrawn.md", content_sha256="f" * 64,
            byte_size=170, observed_at=datetime.now(timezone.utc))
        evidence_assertions.write_assertion(
            conn, tenant_id=TENANT, project_id=PROJECT, document=retracted,
            source_repository=EVIDENCE_REPO, source_path="assertions/withdrawn.md",
            source_revision="e" * 40, source_artifact_id=sidecar_retracted,
            supporting_artifact_ids=[support], accepted_by=PRINCIPAL)
    return UUID(stored["id"])


def main() -> None:
    assertion_id = seed()
    query = "why does the project use PostgreSQL pgvector for vectors"

    print("\n1. Reviewed assertion projection")
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        candidates = github_projection.candidates(
            conn, query, tenant_id=TENANT, project_id=PROJECT, limit=10)
    check("GitHub-native projects search accepted assertions", len(candidates) == 1,
          str([row.get("assertion_key") for row in candidates]))
    check("candidate carries a typed assertion ref", candidates[0]["ref"] ==
          github_projection.assertion_ref(assertion_id))
    check("sidecar and source bodies are absent from candidate text",
          "Review body" not in candidates[0]["content"] and "src/storage.py" not in candidates[0]["content"])

    print("\n2. API and context behaviour")
    searched = api.search_memories(TENANT, PROJECT, q=query, principal_id=PRINCIPAL, limit=5)
    assertion_ref = github_projection.assertion_ref(assertion_id)
    check("memory_search returns the reviewed assertion as evidence",
          searched["count"] == 1 and searched["results"][0]["ref"] == assertion_ref,
          str(searched.get("answerability")))
    expanded = api.search_memories(TENANT, PROJECT, refs=assertion_ref, principal_id=PRINCIPAL)
    check("assertion ref expansion returns immutable provenance, not blob content",
          expanded["count"] == 1 and expanded["results"][0]["kind"] == "assertion"
          and expanded["results"][0]["evidence"] and "Review body" not in str(expanded), str(expanded))
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        pack = context.build_pack(
            conn, query, tenant_id=TENANT, project_id=PROJECT, principal_id=PRINCIPAL)
    items = [item for section in pack["sections"].values() for item in section]
    check("memory_context emits the assertion as an evidence item",
          any(item["ref"] == assertion_ref and item["record_kind"] == "assertion" for item in items),
          str(pack["answerability"]))
    explained = api.explain(TENANT, PROJECT, principal_id=PRINCIPAL, ref=assertion_id)
    check("memory_explain returns the assertion evidence chain",
          explained.get("assertion", {}).get("assertion_key") == "storage"
          and len(explained.get("provenance", [])) == 2)

    print("\n3. No-evidence remains explicit")
    absent = api.search_memories(
        TENANT, PROJECT, q="what is the unrecorded Zanzibar data residency policy",
        principal_id=PRINCIPAL, limit=5)
    check("unrelated GitHub query returns no project evidence", absent["count"] == 0 and
          absent["answerability"]["status"] == "no_relevant_evidence", str(absent))

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
