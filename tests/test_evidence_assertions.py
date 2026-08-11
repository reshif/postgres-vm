"""Reviewed sidecar assertion contract and its RLS boundaries."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, evidence_assertions  # noqa: E402

RUN = uuid.uuid4().hex[:8]
TENANT = UUID("8b120000-0000-0000-0000-0000000000a1")
PROJECT = UUID("8b120000-0000-0000-0000-0000000000a2")
OTHER_PROJECT = UUID("8b120000-0000-0000-0000-0000000000a3")
PRINCIPAL = UUID("8b120000-0000-0000-0000-0000000000a4")
SOURCE_SHA = "1" * 40
SIDE_SHA_A = "2" * 40
SIDE_SHA_B = "3" * 40
SOURCE_REPO = "https://github.com/evidence-assertion-test/service"
EVIDENCE_REPO = "https://github.com/evidence-assertion-test/service-evidence"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def document(revision: str, *, confidence: str = "0.960") -> str:
    return f"""---
id: service-storage
subject: service
predicate: uses
object: PostgreSQL
state: accepted
confidence: {confidence}
evidence:
  - github://github.com/evidence-assertion-test/service@{SOURCE_SHA}:src/storage.py
---
The storage selection is human-reviewed in the evidence repository.
"""


def seed() -> None:
    with db.engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO mem.organizations (id, slug, name) VALUES (:id, :slug, :slug) "
            "ON CONFLICT DO NOTHING"), {"id": str(TENANT), "slug": "assertion-test"})
        for project, slug in ((PROJECT, "assertion-service"), (OTHER_PROJECT, "assertion-other")):
            conn.execute(text(
                "INSERT INTO mem.projects "
                "(id, tenant_id, slug, name, repo_url, source_provider, evidence_repo_url, github_installation_id) "
                "VALUES (:id, :tenant, :slug, :slug, :repo, 'github', :evidence, 77) "
                "ON CONFLICT (id) DO UPDATE SET repo_url = EXCLUDED.repo_url, "
                "evidence_repo_url = EXCLUDED.evidence_repo_url, source_provider = 'github'"),
                {"id": str(project), "tenant": str(TENANT), "slug": slug,
                 "repo": SOURCE_REPO if project == PROJECT else SOURCE_REPO + "-other",
                 "evidence": EVIDENCE_REPO if project == PROJECT else EVIDENCE_REPO + "-other"})
        conn.execute(text(
            "INSERT INTO mem.principals (id, tenant_id, actor, external_id, display_name) "
            "VALUES (:id, :tenant, 'service', 'assertion-test', 'Assertion test') "
            "ON CONFLICT DO NOTHING"), {"id": str(PRINCIPAL), "tenant": str(TENANT)})


def main() -> None:
    print("\n1. Assertion format")
    parsed = evidence_assertions.parse_assertion("assertions/service-storage.md", document(SIDE_SHA_A))
    check("accepted assertion parses into structured fields", parsed.subject == "service" and
          parsed.object_value == "PostgreSQL")
    check("assertion requires immutable Git blob evidence", parsed.evidence[0].revision == SOURCE_SHA)
    try:
        evidence_assertions.parse_assertion("assertions/no-proof.md", "---\nid: no-proof\nsubject: a\npredicate: b\nobject: c\nstate: accepted\n---\n")
        missing_evidence = False
    except evidence_assertions.AssertionFormatError:
        missing_evidence = True
    check("missing evidence is rejected", missing_evidence)
    try:
        evidence_assertions.parse_assertion("assertions/unsafe.md", document(SIDE_SHA_A).replace(
            "src/storage.py", "../secret.txt"))
        unsafe = False
    except evidence_assertions.AssertionFormatError:
        unsafe = True
    check("unsafe evidence paths are rejected", unsafe)

    print("\n2. Provenance and supersession")
    seed()
    now = datetime.now(timezone.utc)
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        support = evidence_assertions.record_git_blob(
            conn, tenant_id=TENANT, project_id=PROJECT, repository_url=SOURCE_REPO,
            revision=SOURCE_SHA, path="src/storage.py",
            content_sha256=hashlib.sha256(b"storage implementation").hexdigest(),
            byte_size=22, observed_at=now)
        sidecar_a = evidence_assertions.record_git_blob(
            conn, tenant_id=TENANT, project_id=PROJECT, repository_url=EVIDENCE_REPO,
            revision=SIDE_SHA_A, path="assertions/service-storage.md",
            content_sha256=hashlib.sha256(document(SIDE_SHA_A).encode()).hexdigest(),
            byte_size=len(document(SIDE_SHA_A).encode()), observed_at=now)
        supports = evidence_assertions.resolve_evidence_artifacts(conn, document=parsed)
        first = evidence_assertions.write_assertion(
            conn, tenant_id=TENANT, project_id=PROJECT, document=parsed,
            source_repository=EVIDENCE_REPO, source_path="assertions/service-storage.md",
            source_revision=SIDE_SHA_A, source_artifact_id=sidecar_a,
            supporting_artifact_ids=supports, accepted_by=PRINCIPAL)

        parsed_b = evidence_assertions.parse_assertion("assertions/service-storage.md", document(SIDE_SHA_B))
        sidecar_b = evidence_assertions.record_git_blob(
            conn, tenant_id=TENANT, project_id=PROJECT, repository_url=EVIDENCE_REPO,
            revision=SIDE_SHA_B, path="assertions/service-storage.md",
            content_sha256=hashlib.sha256(document(SIDE_SHA_B).encode()).hexdigest(),
            byte_size=len(document(SIDE_SHA_B).encode()), observed_at=now)
        second = evidence_assertions.write_assertion(
            conn, tenant_id=TENANT, project_id=PROJECT, document=parsed_b,
            source_repository=EVIDENCE_REPO, source_path="assertions/service-storage.md",
            source_revision=SIDE_SHA_B, source_artifact_id=sidecar_b,
            supporting_artifact_ids=supports, accepted_by=PRINCIPAL)
        states = conn.execute(text(
            "SELECT state, count(*) FROM mem.evidence_assertions WHERE assertion_key = 'service-storage' "
            "GROUP BY state")).all()
        links = conn.execute(text(
            "SELECT role, count(*) FROM mem.assertion_evidence WHERE assertion_id = :id GROUP BY role"),
            {"id": second["id"]}).all()
        memories = conn.execute(text(
            "SELECT count(*) FROM mem.memories WHERE tenant_id = :tenant AND project_id = :project"),
            {"tenant": str(TENANT), "project": str(PROJECT)}).scalar_one()
    check("first assertion carries its supporting artifact", first["evidence_count"] == 1)
    check("new sidecar revision supersedes the previous claim", dict(states) ==
          {"accepted": 1, "superseded": 1}, str(states))
    check("assertion links source and supporting artifacts", dict(links) ==
          {"derived_from": 1, "supports": 1}, str(links))
    check("assertion import does not create a legacy memory", memories == 0, str(memories))

    print("\n3. Scope isolation")
    with db.scoped(TENANT, PRINCIPAL, OTHER_PROJECT) as conn:
        assertions = conn.execute(text("SELECT count(*) FROM mem.evidence_assertions")).scalar_one()
        links = conn.execute(text("SELECT count(*) FROM mem.assertion_evidence")).scalar_one()
    check("another project cannot read reviewed assertions", assertions == 0, str(assertions))
    check("another project cannot read assertion evidence links", links == 0, str(links))

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
