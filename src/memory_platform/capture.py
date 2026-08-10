"""Deterministic capture — the only write path that needs no reviewer.

05-BUILD-PLAN Phase 2 and ADR-0015: "Deterministic capture only. CI outcomes,
merged PRs, commit metadata, tool exit codes. Rule-based classifiers, no LLM.
Capped at trust tier `observed`, never authoritative, 30-day decay."

RULE-BASED IS THE POINT, NOT A LIMITATION. An LLM summarising a CI failure into
project memory is a persistence layer for whatever the build log happened to
contain, and build logs are attacker-influenceable (a test name, a dependency's
banner, a commit message). Everything here is derived from structured fields —
exit codes, branch names, conclusions — and never from free text the pipeline
did not produce itself.

TIER CAPS ARE ENFORCED HERE AND AGAIN IN memories.assign_tier. A CI success is
`verified` because a machine checked it; a commit is `observed` because it only
attests that something happened. Nothing on this path can reach `authoritative`,
which is reserved for reviewed Plane A content.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Connection

from . import memories

log = logging.getLogger("memory.capture")

# Outcome -> (memory type, source_type). source_type drives the tier, so this
# table is the whole trust decision for the capture path.
#
#   ci / deploy / test -> verified   a machine checked an outcome
#   commit / tool      -> observed   something happened, nothing was verified
CI_OUTCOMES: dict[str, tuple[str, str]] = {
    "success": ("success", "ci"),
    "failure": ("failure", "ci"),
    "timed_out": ("failure", "ci"),
    "cancelled": ("episode", "commit"),
    "skipped": ("episode", "commit"),
}

# Failure fingerprints. Deliberately a small, explicit table: the value of a
# captured failure is that "have we seen this before" can match it later, and
# that only works if the same failure produces the same classification every
# time. A fuzzy classifier that drifts is worse than none, because recurrence
# lookups then quietly stop matching.
FAILURE_SIGNATURES: list[tuple[str, re.Pattern[str]]] = [
    ("out-of-memory", re.compile(r"\b(oom|out of memory|exit code 137|killed)\b", re.I)),
    ("timeout", re.compile(r"\b(timed out|timeout|deadline exceeded|etimedout)\b", re.I)),
    ("dependency-resolution", re.compile(
        r"\b(resolutionimpossible|could not find a version|unresolved dependency"
        r"|conflicting dependencies)\b", re.I)),
    ("permission", re.compile(r"\b(permission denied|forbidden|unauthorized|eacces)\b", re.I)),
    ("connection", re.compile(
        r"\b(connection refused|econnrefused|could not connect|no route to host)\b", re.I)),
    ("migration", re.compile(r"\b(alembic|migration|relation .* does not exist)\b", re.I)),
    ("test-failure", re.compile(r"\b(assertionerror|test failed|\d+ failed)\b", re.I)),
    ("lint", re.compile(r"\b(ruff|flake8|eslint|lint error)\b", re.I)),
]

MAX_LOG_EXCERPT = 1500


def classify_failure(log_excerpt: str) -> str:
    """Name a failure class, or 'unclassified'. Never invents a category."""
    for name, pat in FAILURE_SIGNATURES:
        if pat.search(log_excerpt or ""):
            return name
    return "unclassified"


def _clean(text: str, limit: int) -> str:
    """Strip ANSI escapes and control noise from a build log.

    Not sanitisation for safety — the tier cap and the quarantine model do that.
    This is so the stored text is greppable and the digest is readable.
    """
    t = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")
    t = "".join(ch for ch in t if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t[:limit]


def capture_ci_run(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID | None,
    workflow: str,
    conclusion: str,
    repo: str,
    sha: str,
    branch: str = "",
    run_url: str = "",
    log_excerpt: str = "",
    duration_s: float | None = None,
) -> dict[str, Any]:
    """Record the outcome of a CI run.

    Idempotent on content: the same run captured twice does not duplicate,
    because memories.write_memory dedupes on the content hash and the content is
    derived deterministically from the run's fields.
    """
    conclusion = (conclusion or "").strip().lower()
    mtype, source = CI_OUTCOMES.get(conclusion, ("episode", "commit"))
    excerpt = _clean(log_excerpt, MAX_LOG_EXCERPT)
    short = sha[:12] if sha else "unknown"

    if mtype == "failure":
        signature = classify_failure(excerpt)
        title = f"CI {workflow} failed on {branch or 'unknown branch'}: {signature}"
        body = (
            f"Workflow `{workflow}` concluded `{conclusion}` for {repo} at commit "
            f"{short}"
            + (f" on branch {branch}" if branch else "")
            + (f", after {duration_s:.0f}s" if duration_s else "")
            + f". Failure class: {signature}."
            + (f"\n\nLog excerpt:\n{excerpt}" if excerpt else "")
        )
    elif mtype == "success":
        signature = "green"
        title = f"CI {workflow} passed on {branch or 'unknown branch'}"
        body = (
            f"Workflow `{workflow}` passed for {repo} at commit {short}"
            + (f" on branch {branch}" if branch else "")
            + (f" in {duration_s:.0f}s" if duration_s else "")
            + "."
        )
    else:
        signature = conclusion or "unknown"
        title = f"CI {workflow} {conclusion or 'ended'} on {branch or 'unknown branch'}"
        body = f"Workflow `{workflow}` concluded `{conclusion}` for {repo} at {short}."

    result = memories.write_memory(
        conn,
        tenant_id=tenant_id, project_id=project_id, principal_id=principal_id,
        mtype=mtype, title=title[:200], content=body,
        source_type=source,
        memory_key=f"ci:{workflow}:{short}:{conclusion}",
        source_uri=run_url or repo, source_version=sha or None,
        metadata={
            "capture": "ci", "workflow": workflow, "conclusion": conclusion,
            "branch": branch, "signature": signature, "repo": repo,
            "duration_s": duration_s,
        },
    )
    log.info("captured CI %s/%s -> %s (%s)", workflow, conclusion,
             result.get("tier"), signature)
    return {**result, "signature": signature, "type": mtype}


def capture_commit(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID | None,
    sha: str,
    message: str,
    author: str = "",
    files_changed: int | None = None,
    repo: str = "",
) -> dict[str, Any]:
    """Record commit metadata as an episode.

    Only the SUBJECT line is stored, never the full body. A commit message is
    free text written by whoever pushed, and the body is where a prompt-injection
    payload would go — an episode that quietly carries "ignore previous
    instructions" into every future context pack is exactly the failure ADR-0015
    exists to prevent. `observed`, never higher.
    """
    subject = _clean((message or "").splitlines()[0] if message else "", 200)
    short = sha[:12] if sha else "unknown"
    body = (
        f"Commit {short}"
        + (f" by {author}" if author else "")
        + (f" touching {files_changed} file(s)" if files_changed is not None else "")
        + f": {subject}"
    )
    return memories.write_memory(
        conn,
        tenant_id=tenant_id, project_id=project_id, principal_id=principal_id,
        mtype="episode", title=f"Commit {short}: {subject}"[:200], content=body,
        source_type="commit",
        memory_key=f"commit:{short}",
        source_uri=repo or None, source_version=sha or None,
        metadata={"capture": "commit", "author": author,
                  "files_changed": files_changed},
    )


def capture_tool_result(
    conn: Connection,
    *,
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID | None,
    tool: str,
    exit_code: int,
    command: str = "",
    output_excerpt: str = "",
) -> dict[str, Any]:
    """Record a tool invocation outcome (05-BUILD-PLAN: "tool exit codes")."""
    ok = exit_code == 0
    excerpt = _clean(output_excerpt, MAX_LOG_EXCERPT)
    signature = "ok" if ok else classify_failure(excerpt)
    title = (f"{tool} succeeded" if ok
             else f"{tool} failed (exit {exit_code}): {signature}")
    body = (
        f"`{command or tool}` exited {exit_code}."
        + (f" Failure class: {signature}." if not ok else "")
        + (f"\n\nOutput:\n{excerpt}" if excerpt else "")
    )
    return memories.write_memory(
        conn,
        tenant_id=tenant_id, project_id=project_id, principal_id=principal_id,
        mtype="success" if ok else "failure",
        title=title[:200], content=body,
        # `tool`, not `ci`: a local tool run is not a verified outcome, it is an
        # observation. Only a pipeline this project controls earns `verified`.
        source_type="tool",
        memory_key=f"tool:{tool}:{signature}:{abs(hash(command)) % 10**8}",
        metadata={"capture": "tool", "tool": tool, "exit_code": exit_code,
                  "signature": signature},
    )
