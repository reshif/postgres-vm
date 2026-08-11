"""GitHub-native projects form a unique source/evidence routing boundary."""
from __future__ import annotations

import sys
import uuid

from fastapi import HTTPException
from sqlalchemy import text

sys.path.insert(0, "/app/src")
from memory_platform import api, db  # noqa: E402


RUN = uuid.uuid4().hex[:8]
ORG = f"github-binding-{RUN}"
SOURCE = f"https://github.com/{ORG}/service.git"
EVIDENCE = f"https://github.com/{ORG}/service-evidence.git"
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def rejected(req: api.RegisterProject, expected: int) -> bool:
    try:
        api.register_project(req)
    except HTTPException as exc:
        return exc.status_code == expected
    return False


def request(slug: str, *, source: str = SOURCE, evidence: str = EVIDENCE) -> api.RegisterProject:
    return api.RegisterProject(
        org_slug=ORG, project_slug=slug, name=slug, repo_url=source,
        source_provider="github", evidence_repo_url=evidence,
        github_installation_id=42, git_default_branch="main")


def main() -> None:
    print("\n1. Required GitHub binding fields")
    check("source and evidence repositories cannot be the same", rejected(
        request("same-repo", evidence=SOURCE), 422))
    check("a GitHub project requires its App installation", rejected(api.RegisterProject(
        org_slug=ORG, project_slug="missing-installation", repo_url=SOURCE,
        source_provider="github", evidence_repo_url=EVIDENCE), 422))

    print("\n2. Unique repository ownership")
    first = api.register_project(request("service"))
    with db.engine().connect() as conn:
        row = conn.execute(text(
            "SELECT source_provider, evidence_repo_url, github_installation_id, git_default_branch "
            "FROM mem.projects WHERE id = :id"), {"id": first["project_id"]}).mappings().one()
    check("registration persists the complete GitHub binding",
          row["source_provider"] == "github" and row["evidence_repo_url"] == EVIDENCE
          and row["github_installation_id"] == 42 and row["git_default_branch"] == "main", str(row))
    check("the same source cannot bind a second GitHub project", rejected(
        request("duplicate-source", evidence=f"https://github.com/{ORG}/other-evidence.git"), 409))
    check("an evidence repository cannot become another project's source", rejected(
        request("role-collision", source=EVIDENCE,
                evidence=f"https://github.com/{ORG}/role-collision-evidence.git"), 409))
    repeat = api.register_project(request("service"))
    check("re-registration remains idempotent", repeat["project_id"] == first["project_id"]
          and not repeat["created"])

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
