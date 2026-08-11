"""GitHub-native project initialization does not recreate the legacy checkout."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, "/app/src")
from memory_platform import cli  # noqa: E402


RUN = uuid.uuid4().hex[:8]
ORG = f"github-cli-{RUN}"
PROJECT = f"service-{RUN}"
SOURCE = f"https://github.com/{ORG}/{PROJECT}.git"
EVIDENCE = f"https://github.com/{ORG}/{PROJECT}-evidence.git"
results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def call(argv: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(argv)
    return code, output.getvalue()


def main() -> None:
    old_cwd = Path.cwd()
    original_req = cli._req
    calls: list[dict] = []

    def fake_req(method: str, path: str, body: dict | None = None, **_kwargs):
        calls.append({"method": method, "path": path, "body": body})
        return 201, {
            "tenant_id": "11111111-1111-1111-1111-111111111111",
            "project_id": "22222222-2222-2222-2222-222222222222",
            "principal_id": "33333333-3333-3333-3333-333333333333",
            "created": True,
        }

    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "remote", "add", "origin", SOURCE], cwd=root, check=True)
            os.chdir(root)
            cli._req = fake_req

            print("\n1. GitHub-native initialization")
            code, output = call([
                "init", "--github", "--org", ORG, "--project", PROJECT,
                "--evidence-repo", EVIDENCE, "--installation-id", "42",
            ])
            binding_path = root / ".memory-platform" / "binding.json"
            binding = json.loads(binding_path.read_text("utf-8")) if binding_path.exists() else {}
            request = calls[0]["body"] if calls else {}
            check("registers the source and evidence repository as one binding",
                  code == 0 and request == {
                      "org_slug": ORG, "project_slug": PROJECT, "name": PROJECT,
                      "repo_url": SOURCE, "source_provider": "github",
                      "evidence_repo_url": EVIDENCE, "github_installation_id": 42,
                      "git_default_branch": "main",
                  }, output[:180])
            check("writes the GitHub-native local binding outside .memory",
                  binding.get("source_provider") == "github"
                  and binding.get("evidence_repo_url") == EVIDENCE
                  and binding.get("git_default_branch") == "main"
                  and not (root / ".memory").exists())
            check("adds an agent instruction that preserves Git as authority",
                  "GitHub-native knowledge project" in (root / "AGENTS.md").read_text("utf-8"))
            check("later CLI commands resolve the GitHub-native binding",
                  cli.load_binding(root).get("project_id") == binding.get("project_id"))
    finally:
        cli._req = original_req
        os.chdir(old_cwd)

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
