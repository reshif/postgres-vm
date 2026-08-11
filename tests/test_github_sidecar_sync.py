"""Exact-SHA sidecar synchronization before it is wired to a real GitHub App."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import sys
import uuid
from uuid import UUID

from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import db, evidence_assertions  # noqa: E402
from memory_platform.github_client import GitBlob  # noqa: E402

TENANT = UUID("8b130000-0000-0000-0000-0000000000a1")
PROJECT = UUID("8b130000-0000-0000-0000-0000000000a2")
PRINCIPAL = UUID("8b130000-0000-0000-0000-0000000000a3")
SOURCE_REPO = "https://github.com/sidecar-sync-test/service"
EVIDENCE_REPO = "https://github.com/sidecar-sync-test/service-evidence"
SOURCE_SHA = "4" * 40
SIDE_SHA = "5" * 40

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def blob(repo: str, revision: str, path: str, content: str, git_sha: str) -> GitBlob:
    raw = content.encode()
    return GitBlob(repo, revision, path, git_sha, content,
                   hashlib.sha256(raw).hexdigest(), len(raw))


ASSERTION = f"""---
id: pg-storage
subject: service
predicate: uses
object: PostgreSQL
state: accepted
evidence:
  - github://github.com/sidecar-sync-test/service@{SOURCE_SHA}:src/storage.py
---
Review body is intentionally not persisted as a free-form memory.
"""


class FakeGitHub:
    def list_blobs(self, **kwargs):
        assert kwargs["repository_url"] == EVIDENCE_REPO
        assert kwargs["revision"] == SIDE_SHA
        return [{"path": "assertions/pg-storage.md", "sha": "sidecar-blob", "size": len(ASSERTION)}]

    def get_text_blob(self, **kwargs):
        return blob(EVIDENCE_REPO, SIDE_SHA, kwargs["path"], ASSERTION, kwargs["git_sha"])

    def get_text_file(self, **kwargs):
        assert kwargs["repository_url"] == SOURCE_REPO
        assert kwargs["revision"] == SOURCE_SHA
        return blob(SOURCE_REPO, SOURCE_SHA, kwargs["path"], "DATABASE_URL = 'postgresql://'\n", "source-blob")


def seed() -> None:
    with db.engine().begin() as conn:
        conn.execute(text(
            "INSERT INTO mem.organizations (id, slug, name) VALUES (:id, 'sidecar-sync', 'sidecar-sync') "
            "ON CONFLICT DO NOTHING"), {"id": str(TENANT)})
        conn.execute(text(
            "INSERT INTO mem.projects "
            "(id, tenant_id, slug, name, repo_url, source_provider, evidence_repo_url, github_installation_id) "
            "VALUES (:id, :tenant, 'sidecar-sync', 'sidecar-sync', :source, 'github', :evidence, 77) "
            "ON CONFLICT (id) DO UPDATE SET repo_url = EXCLUDED.repo_url, "
            "evidence_repo_url = EXCLUDED.evidence_repo_url, source_provider = 'github'"),
            {"id": str(PROJECT), "tenant": str(TENANT), "source": SOURCE_REPO, "evidence": EVIDENCE_REPO})
        conn.execute(text(
            "INSERT INTO mem.principals (id, tenant_id, actor, external_id, display_name) "
            "VALUES (:id, :tenant, 'service', 'sidecar-sync', 'Sidecar sync') ON CONFLICT DO NOTHING"),
            {"id": str(PRINCIPAL), "tenant": str(TENANT)})


def main() -> None:
    print("\n1. Prepare exact Git objects")
    prepared = evidence_assertions.prepare_sidecar_sync(
        FakeGitHub(), source_repository=SOURCE_REPO, evidence_repository=EVIDENCE_REPO,
        revision=SIDE_SHA, installation_id=77, max_blob_bytes=1024, max_files=5)
    check("sidecar fetch prepares one reviewed assertion", len(prepared) == 1)
    check("supporting source blob keeps its cited SHA", prepared[0].supporting_blobs[0].revision == SOURCE_SHA)
    check("assertion body is not stored in the structured claim", "Review body" not in
          str(prepared[0].document))

    print("\n2. Atomic persistence")
    seed()
    with db.scoped(TENANT, PRINCIPAL, PROJECT) as conn:
        report = evidence_assertions.persist_sidecar_sync(
            conn, tenant_id=TENANT, project_id=PROJECT, evidence_repository=EVIDENCE_REPO,
            evidence_revision=SIDE_SHA, observed_at=datetime.now(timezone.utc),
            prepared=prepared, accepted_by=PRINCIPAL)
        row = conn.execute(text(
            "SELECT state, source_revision FROM mem.evidence_assertions WHERE assertion_key = 'pg-storage' "
            "ORDER BY recorded_at DESC LIMIT 1")).mappings().one()
        artifacts = conn.execute(text(
            "SELECT count(*) FROM mem.evidence_artifacts WHERE kind = 'git_blob' "
            "AND project_id = :project"), {"project": str(PROJECT)}).scalar_one()
        memories = conn.execute(text(
            "SELECT count(*) FROM mem.memories WHERE project_id = :project"),
            {"project": str(PROJECT)}).scalar_one()
    check("one accepted assertion is persisted", report == {"assertions": 1, "supporting_blobs": 1}, str(report))
    check("assertion provenance is the evidence repository commit", row["source_revision"] == SIDE_SHA)
    check("assertion and source blobs become separate artifacts", artifacts == 2, str(artifacts))
    check("sidecar sync does not create a legacy memory", memories == 0, str(memories))

    print("\n3. Bound source references")
    foreign = ASSERTION.replace("sidecar-sync-test/service@", "other-org/other@")

    class ForeignGitHub(FakeGitHub):
        def get_text_blob(self, **kwargs):
            return blob(EVIDENCE_REPO, SIDE_SHA, kwargs["path"], foreign, kwargs["git_sha"])

    try:
        evidence_assertions.prepare_sidecar_sync(
            ForeignGitHub(), source_repository=SOURCE_REPO, evidence_repository=EVIDENCE_REPO,
            revision=SIDE_SHA, installation_id=77, max_blob_bytes=1024, max_files=5)
        foreign_rejected = False
    except evidence_assertions.AssertionFormatError:
        foreign_rejected = True
    check("sidecar cannot cite another project's repository", foreign_rejected)

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
