"""Project-facing CLI acceptance against a real API and temporary Git repository."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, "/app/src")
from memory_platform import cli  # noqa: E402


API = "http://localhost:8080"
RUN = uuid.uuid4().hex[:8]
ORG = f"cli-{RUN}"
PROJECT = f"workspace-{RUN}"
MARKER = f"cli-search-{RUN}"

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def call(argv: list[str]) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(argv)
    return code, output.getvalue()


def post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        API + path, method="POST", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def main() -> None:
    cli.API = API
    old_cwd = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "remote", "add", "origin",
                            f"https://github.example/{ORG}/{PROJECT}.git"],
                           cwd=root, check=True)
            os.chdir(root)

            print("\n1. Initialization")
            code, output = call(["init", "--org", ORG, "--project", PROJECT])
            binding_path = root / ".memory" / "binding.json"
            binding = json.loads(binding_path.read_text("utf-8")) if binding_path.exists() else {}
            check("init registers a scoped project and writes a local binding",
                  code == 0 and all(binding.get(key) for key in
                                    ("tenant_id", "project_id", "principal_id")), output[:120])
            check("init scaffolds knowledge, agent, and MCP client files",
                  (root / ".memory" / "project.yaml").is_file()
                  and (root / ".memory" / "decisions").is_dir()
                  and (root / ".mcp.json").is_file()
                  and "## Project memory" in (root / "AGENTS.md").read_text("utf-8"))

            code, output = call(["init", "--org", ORG, "--project", PROJECT])
            check("re-running init preserves the existing project binding",
                  code == 0 and "(existing)" in output
                  and json.loads(binding_path.read_text("utf-8"))["project_id"] == binding["project_id"])

            print("\n2. Scoped retrieval commands")
            post("/v1/memories", {
                "tenant_id": binding["tenant_id"], "project_id": binding["project_id"],
                "principal_id": binding["principal_id"], "type": "decision",
                "title": f"CLI marker {MARKER}",
                "content": f"The exact searchable marker is {MARKER}.",
                "source_type": "human",
            })
            code, output = call(["search", MARKER])
            check("search uses the generated binding rather than caller IDs",
                  code == 0 and MARKER in output, output[:140])
            code, output = call(["why", MARKER])
            check("why is a working rationale-oriented search command",
                  code == 0 and MARKER in output, output[:140])
            code, output = call(["status"])
            check("status reports the bound project and API health",
                  code == 0 and f"{ORG}/{PROJECT}" in output and "platform" in output,
                  output[:160])
    finally:
        os.chdir(old_cwd)

    failed = [name for ok, name in results if not ok]
    print(f"\n{'=' * 62}\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        for name in failed:
            print(f"  FAILED: {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
