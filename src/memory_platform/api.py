"""Core API — Phase 1 skeleton.

Endpoints exist to prove the wiring, not to implement the product. The context
engine (03/05 in the blueprint) lands in Phase 3.
"""
from __future__ import annotations

import logging
import re

from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from sqlalchemy import text

from . import __version__, context, db, limits, memories
from .config import settings

log = logging.getLogger("memory.api")
app = FastAPI(title="Memory Platform API", version=__version__)

# Exposes GET /metrics, which ops/prometheus.yml already scrapes as the
# `memory-api` job. The probes are excluded from the histograms on purpose: the
# compose healthcheck hits /healthz every 10s and Prometheus hits /metrics every
# scrape interval, so leaving them in makes p99 latency a measure of how often we
# health-check ourselves rather than of anything a user experiences.
Instrumentator(
    excluded_handlers=["/metrics", "/healthz", "/readyz"],
).instrument(app).expose(app, include_in_schema=False)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness only. Deliberately does not touch the database."""
    return {"status": "ok", "version": __version__, "role": settings().role}


@app.get("/readyz")
def readyz() -> JSONResponse:
    """Readiness, including the isolation self-test.

    A cold embedder is reported as degraded, not unready: retrieval falls back to
    the lexical arm rather than failing closed (ADR-0008).
    """
    checks: dict = {}
    ok = True

    try:
        checks["database"] = {"ok": db.ping()}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "error": str(exc)}
        ok = False

    if checks["database"].get("ok"):
        try:
            iso = db.isolation_selftest()
            checks["isolation"] = iso
            if not iso["pass"]:
                ok = False
                log.error("ISOLATION FAILURE: unscoped query returned rows")
        except Exception as exc:  # noqa: BLE001
            checks["isolation"] = {"ok": False, "error": str(exc)}
            ok = False

    # Health path is provider-specific: TEI serves /health, Ollama does not serve
    # it at all (it 404s) and /api/tags is the cheap liveness endpoint. Probing
    # the wrong one reports a perfectly healthy embedder as permanently down.
    _EMBED_HEALTH_PATH = {"local": "/health", "ollama": "/api/tags"}
    try:
        provider = settings().embedding_provider
        path = _EMBED_HEALTH_PATH.get(provider, "/health")
        r = httpx.get(f"{settings().embedding_url}{path}", timeout=2.0)
        checks["embeddings"] = {"ok": r.status_code == 200, "provider": provider}
    except Exception as exc:  # noqa: BLE001
        # Never fails the readiness gate: ADR-0008 has retrieval degrade to the
        # lexical arm rather than closing. A cold embedder is degraded, not down.
        checks["embeddings"] = {"ok": False, "degraded": True, "error": str(exc)}

    return JSONResponse(
        {"ready": ok, "checks": checks},
        status_code=200 if ok else 503,
    )


def _guard_read(tenant: str) -> None:
    """429 for rate limits, 503 for system overload — different problems.

    A 429 tells the client to slow down; a 503 tells it the fault is ours and to
    retry later. Returning the wrong one makes a well-behaved client either back
    off from a transient blip or hammer a limit that will never clear.
    """
    try:
        limits.check_read(tenant)
    except limits.RateLimited as exc:
        raise HTTPException(429, str(exc),
                            headers={"Retry-After": str(exc.retry_after)}) from exc


def _guard_write(tenant: str) -> None:
    try:
        limits.check_write(tenant)
    except limits.RateLimited as exc:
        raise HTTPException(429, str(exc),
                            headers={"Retry-After": str(exc.retry_after)}) from exc


class WriteRequest(BaseModel):
    """Note what is absent: `tier`, `confidence` and `status`.

    They are assigned server-side from source_type (memories.assign_tier) and are
    not accepted from callers — tier is what retrieval filters and ranks on, so a
    caller able to set it could promote its own text to authoritative.
    """
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None
    type: str
    title: str
    content: str
    source_type: str
    memory_key: str | None = None
    source_uri: str | None = None
    source_version: str | None = None
    metadata: dict | None = None


@app.post("/v1/memories", status_code=201)
def write_memory(req: WriteRequest) -> dict:
    _guard_write(str(req.tenant_id))
    try:
        with db.scoped(req.tenant_id, req.principal_id or req.tenant_id, req.project_id) as conn:
            limits.check_backpressure(conn)
            limits.check_quota(conn, str(req.tenant_id))
            return memories.write_memory(
                conn,
                tenant_id=req.tenant_id, project_id=req.project_id,
                principal_id=req.principal_id, mtype=req.type, title=req.title,
                content=req.content, source_type=req.source_type,
                memory_key=req.memory_key, source_uri=req.source_uri,
                source_version=req.source_version, metadata=req.metadata,
            )
    except limits.Overloaded as exc:
        raise HTTPException(503, str(exc),
                            headers={"Retry-After": str(exc.retry_after)}) from exc
    except limits.QuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/v1/search")
def search_memories(
    tenant_id: UUID,
    project_id: UUID,
    q: str = "",
    refs: str = "",
    principal_id: UUID | None = None,
    limit: int = 8,
) -> dict:
    """Search, or expand refs from a context pack.

    Packs are digest-first: they emit a ref per item and the agent fetches full
    text for the few it needs (blueprint §5.4 rule 2). Ref expansion therefore
    belongs on the same tool, not a separate one — ADR-0003 cut the surface to
    four tools precisely to avoid a `memory_get` that does only this.
    """
    ref_list = [r.strip() for r in refs.split(",") if r.strip()]
    if ref_list:
        with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
            rows = conn.execute(text(
                "SELECT id, title, content, digest, type::text AS type, "
                "       tier::text AS tier, source_uri, source_version "
                "  FROM mem.memories WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": "{" + ",".join(ref_list) + "}"}).mappings().all()
        return {"refs": ref_list, "count": len(rows),
                "results": [{**dict(r), "id": str(r["id"])} for r in rows]}

    if not q:
        raise HTTPException(422, "pass q (a query) or refs (ids to expand)")
    _guard_read(str(tenant_id))
    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        hits = memories.search(conn, q, limit=limit,
                               tenant_id=tenant_id, project_id=project_id)
    return {
        "query": q,
        "count": len(hits),
        # Surfaced, not hidden: a caller seeing lexical-only results should know
        # the vector arm was unavailable rather than assume nothing matched.
        "degraded": bool(hits and hits[0].get("degraded")),
        "results": [{**h, "id": str(h["id"])} for h in hits],
    }


class ContextRequest(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None
    task: str
    token_budget: int = 4000
    window_fill_pct: float | None = None
    include_unverified: bool = False


@app.post("/v1/context")
def build_context(req: ContextRequest) -> dict:
    _guard_read(str(req.tenant_id))
    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id, req.project_id) as conn:
        return context.build_pack(
            conn, req.task, tenant_id=req.tenant_id, project_id=req.project_id,
            principal_id=req.principal_id, token_budget=req.token_budget,
            window_fill_pct=req.window_fill_pct,
            include_unverified=req.include_unverified,
        )


@app.get("/v1/explain")
def explain(
    tenant_id: UUID,
    project_id: UUID,
    principal_id: UUID | None = None,
    ref: UUID | None = None,
    pack_id: str | None = None,
) -> dict:
    """Provenance for a memory, or the score decomposition for a past pack.

    ADR-0003 calls this the trust surface. It answers from what was RECORDED at
    the time — the stored retrieval_event — rather than recomputing. Recomputing
    would use today's ranking profile to explain yesterday's ordering, which
    answers a different question than the one being asked.
    """
    if not ref and not pack_id:
        raise HTTPException(422, "pass ref (a memory id) or pack_id")

    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        if pack_id:
            row = conn.execute(text(
                "SELECT pack_id, tool, query_text, plan, arm_results, fused, dropped, "
                "       returned_ids, token_count, ranking_profile, latency_ms, created_at "
                "  FROM mem.retrieval_events WHERE pack_id = :p"), {"p": pack_id}
            ).mappings().one_or_none()
            if not row:
                raise HTTPException(404, f"no retrieval event for pack {pack_id}")
            d = dict(row)
            d["returned_ids"] = [str(i) for i in (d["returned_ids"] or [])]
            d["created_at"] = d["created_at"].isoformat()
            return d

        mem = conn.execute(text(
            "SELECT id, memory_key, title, digest, type::text AS type, tier::text AS tier, "
            "       status::text AS status, confidence, source_type, source_uri, "
            "       source_version, recorded_at, valid_at::text AS valid_at, "
            "       token_cost, metadata "
            "  FROM mem.memories WHERE id = :i"), {"i": str(ref)}).mappings().one_or_none()
        if not mem:
            # Indistinguishable from "exists in another tenant" on purpose: a 404
            # that means "not yours" is an existence oracle.
            raise HTTPException(404, "no such memory in this scope")

        versions = conn.execute(text(
            "SELECT version, operation, changed_at FROM mem.memory_versions "
            " WHERE memory_id = :i ORDER BY version"), {"i": str(ref)}).mappings().all()
        supers = conn.execute(text(
            "SELECT old_id, new_id, reason, created_at FROM mem.memory_supersessions "
            " WHERE new_id = :i OR old_id = :i"), {"i": str(ref)}).mappings().all()

        d = dict(mem)
        d["id"] = str(d["id"])
        d["recorded_at"] = d["recorded_at"].isoformat()
        return {
            "memory": d,
            "provenance": (
                f"{d['source_type']}:{d['source_uri']}@{d['source_version']}"
                if d.get("source_uri") else d["source_type"]
            ),
            "versions": [
                {**dict(v), "changed_at": v["changed_at"].isoformat()} for v in versions
            ],
            "supersessions": [
                {"old_id": str(s["old_id"]), "new_id": str(s["new_id"]),
                 "reason": s["reason"], "created_at": s["created_at"].isoformat()}
                for s in supers
            ],
        }


class IngestRequest(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None
    repo_path: str | None = None


@app.post("/v1/ingest")
def ingest_now(req: IngestRequest) -> dict:
    """Reconcile the .memory/ tree immediately — the webhook half of ingestion.

    A git webhook calls this so a merged ADR is retrievable in seconds rather than
    at the next poll. The scheduler's poll loop still runs: a webhook that is
    missed, retried into a 500, or fired while the API was restarting would
    otherwise leave the platform permanently stale with nothing indicating it.
    Idempotent, so both paths firing for the same commit is harmless.
    """
    from pathlib import Path as _Path

    from . import ingest as _ingest

    root = _Path(req.repo_path or settings().ingest_repo_path)
    if not (root / ".memory").is_dir():
        raise HTTPException(404, f"no .memory/ directory under {root}")

    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id, req.project_id) as conn:
        report = _ingest.ingest_tree(
            conn, root, tenant_id=req.tenant_id, project_id=req.project_id,
            principal_id=req.principal_id)
    return {
        **report.summary(),
        "created_files": report.created,
        "archived_keys": report.archived,
        # Rejections are returned, not swallowed: a webhook caller that gets 200
        # and an empty body cannot tell "nothing changed" from "your commit had a
        # credential in it and was refused".
        "rejected": [{"path": p, "reason": r} for p, r in report.rejected],
    }


class RegisterProject(BaseModel):
    org_slug: str
    project_slug: str
    name: str | None = None
    repo_url: str | None = None


@app.post("/v1/projects", status_code=201)
def register_project(req: RegisterProject) -> dict:
    """Register (or return) a project. Idempotent on (org_slug, project_slug).

    This is what `memory init` calls. Registration is deliberately server-side:
    a client that could mint its own project ids could also name someone else's,
    and project binding is an authorization boundary (ADR-0004), not a label.
    """
    from uuid import uuid4

    with db.engine().begin() as conn:
        org = conn.execute(text(
            "INSERT INTO mem.organizations (id, slug, name) VALUES (:i, :s, :s) "
            "ON CONFLICT (slug) DO UPDATE SET slug = EXCLUDED.slug RETURNING id"),
            {"i": str(uuid4()), "s": req.org_slug}).scalar_one()

        existing = conn.execute(text(
            "SELECT id, repo_url FROM mem.projects "
            " WHERE tenant_id = :t AND slug = :s"),
            {"t": str(org), "s": req.project_slug}).mappings().one_or_none()

        if existing:
            # Late-added remote: fill it in, but never silently repoint a project
            # that already names a different repository.
            if req.repo_url and not existing["repo_url"]:
                conn.execute(text("UPDATE mem.projects SET repo_url = :r WHERE id = :i"),
                             {"r": req.repo_url, "i": str(existing["id"])})
            elif req.repo_url and existing["repo_url"] != req.repo_url:
                raise HTTPException(409, (
                    f"project {req.org_slug}/{req.project_slug} is already bound to "
                    f"{existing['repo_url']}. Refusing to repoint it — pick a different "
                    "project slug, or update the binding deliberately."))
            project = existing["id"]
        else:
            project = conn.execute(text(
                "INSERT INTO mem.projects (id, tenant_id, slug, name, repo_url) "
                "VALUES (:i, :t, :s, :n, :r) RETURNING id"),
                {"i": str(uuid4()), "t": str(org), "s": req.project_slug,
                 "n": req.name or req.project_slug, "r": req.repo_url}).scalar_one()

        principal = conn.execute(text(
            "INSERT INTO mem.principals (id, tenant_id, actor, external_id, display_name) "
            "VALUES (:i, :t, 'service', :e, :d) "
            "ON CONFLICT (tenant_id, actor, external_id) DO UPDATE "
            "   SET display_name = EXCLUDED.display_name "
            "RETURNING id"),
            {"i": str(uuid4()), "t": str(org), "e": f"cli:{req.project_slug}",
             "d": f"{req.project_slug} CLI"}).scalar_one()

    return {"tenant_id": str(org), "project_id": str(project),
            "principal_id": str(principal), "created": not existing}


@app.get("/v1/projects/resolve")
def resolve_project(repo_url: str) -> dict:
    """Resolve a git remote to exactly one project, or refuse.

    05-BUILD-PLAN Phase 2: "ambiguous binding is an error with a fix
    instruction, never a fallback to a broader scope."

    That rule is the whole point. The tempting behaviour when a remote matches
    two projects is to pick the first, or widen to the org — both silently mix
    one project's memory into another's context, which is the failure the
    isolation model exists to prevent, arriving through the front door with
    valid credentials.
    """
    norm = _normalise_remote(repo_url)
    with db.engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT p.id, p.tenant_id, p.slug, p.repo_url, o.slug AS org "
            "  FROM mem.projects p JOIN mem.organizations o ON o.id = p.tenant_id "
            " WHERE p.repo_url IS NOT NULL")).mappings().all()

    matches = [r for r in rows if _normalise_remote(r["repo_url"] or "") == norm]
    if not matches:
        raise HTTPException(404, (
            f"no project is bound to {repo_url}. Run `memory init` in this "
            "repository to register it."))
    if len(matches) > 1:
        where = ", ".join(f"{m['org']}/{m['slug']}" for m in matches)
        raise HTTPException(409, (
            f"{len(matches)} projects claim {repo_url}: {where}. Binding is "
            "ambiguous and will not be guessed — remove the duplicate "
            "registration, or bind explicitly with MEMORY_DEV_PROJECT_ID."))

    m = matches[0]
    return {"tenant_id": str(m["tenant_id"]), "project_id": str(m["id"]),
            "org_slug": m["org"], "project_slug": m["slug"], "repo_url": m["repo_url"]}


def _normalise_remote(url: str) -> str:
    """git@host:org/repo.git and https://host/org/repo are the same repository.

    Without this, cloning over SSH and over HTTPS registers two projects for one
    codebase, and the resolver then reports an ambiguous binding for a repo the
    user only ever registered once.
    """
    u = (url or "").strip().lower().rstrip("/")
    u = re.sub(r"^(https?://|git\+ssh://|ssh://)", "", u)
    u = re.sub(r"^git@", "", u)
    u = u.replace(":", "/", 1) if u.startswith(("github.com", "gitlab.com", "bitbucket.org")) or "@" not in u else u
    u = re.sub(r"\.git$", "", u)
    return u


class CaptureCI(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None
    workflow: str
    conclusion: str
    repo: str = ""
    sha: str = ""
    branch: str = ""
    run_url: str = ""
    log_excerpt: str = ""
    duration_s: float | None = None


@app.post("/v1/capture/ci", status_code=201)
def capture_ci(req: CaptureCI) -> dict:
    """Deterministic capture of a CI outcome (05-BUILD-PLAN Phase 2).

    Note what the caller cannot set: `tier`, `type`, or `status`. All three are
    derived from `conclusion` by a fixed table. A pipeline that could declare its
    own run authoritative would make the trust lattice decorative.
    """
    from . import capture as _capture

    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id, req.project_id) as conn:
        return _capture.capture_ci_run(
            conn, tenant_id=req.tenant_id, project_id=req.project_id,
            principal_id=req.principal_id, workflow=req.workflow,
            conclusion=req.conclusion, repo=req.repo, sha=req.sha,
            branch=req.branch, run_url=req.run_url,
            log_excerpt=req.log_excerpt, duration_s=req.duration_s)


class CaptureCommit(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None
    sha: str
    message: str
    author: str = ""
    files_changed: int | None = None
    repo: str = ""


@app.post("/v1/capture/commit", status_code=201)
def capture_commit(req: CaptureCommit) -> dict:
    from . import capture as _capture

    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id, req.project_id) as conn:
        return _capture.capture_commit(
            conn, tenant_id=req.tenant_id, project_id=req.project_id,
            principal_id=req.principal_id, sha=req.sha, message=req.message,
            author=req.author, files_changed=req.files_changed, repo=req.repo)


class CaptureTool(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None
    tool: str
    exit_code: int
    command: str = ""
    output_excerpt: str = ""


@app.post("/v1/capture/tool", status_code=201)
def capture_tool(req: CaptureTool) -> dict:
    from . import capture as _capture

    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id, req.project_id) as conn:
        return _capture.capture_tool_result(
            conn, tenant_id=req.tenant_id, project_id=req.project_id,
            principal_id=req.principal_id, tool=req.tool,
            exit_code=req.exit_code, command=req.command,
            output_excerpt=req.output_excerpt)


class DetectConflicts(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None


@app.post("/v1/conflicts/detect")
def detect_conflicts(req: DetectConflicts) -> dict:
    """Run deterministic conflict detection over the project's active memories."""
    from . import conflicts as _conflicts

    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id, req.project_id) as conn:
        return _conflicts.detect(conn, tenant_id=req.tenant_id, project_id=req.project_id)


@app.get("/v1/conflicts")
def list_conflicts(tenant_id: UUID, project_id: UUID,
                   principal_id: UUID | None = None, limit: int = 20) -> dict:
    from . import conflicts as _conflicts

    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        items = _conflicts.unresolved(conn, tenant_id=tenant_id,
                                      project_id=project_id, limit=limit)
    return {"count": len(items), "conflicts": items}


class MaintenanceRequest(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID | None = None


@app.post("/v1/maintenance")
def run_maintenance(req: MaintenanceRequest) -> dict:
    from . import maintenance as _m

    with db.scoped(req.tenant_id, req.principal_id or req.tenant_id, req.project_id) as conn:
        return _m.run_all(conn, tenant_id=req.tenant_id, project_id=req.project_id)


class FeedbackRequest(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID
    signal: str
    memory_id: UUID | None = None
    pack_id: str | None = None
    weight: float = 1.0
    note: str | None = None


@app.post("/v1/feedback", status_code=201)
def submit_feedback(req: FeedbackRequest) -> dict:
    """Record a usefulness signal (ADR-0009).

    Feedback is ADVISORY. It moves `utility`, which is one weighted term in the
    ranking model and only counts once a memory has been seen in enough
    independent sessions. It cannot move `tier`, which is the difference between
    "people found this handy" and "this was reviewed" — a system where enough
    upvotes promote a claim to authoritative has no trust lattice at all.
    """
    valid = {"useful", "irrelevant", "wrong", "missing", "pin", "unpin"}
    if req.signal not in valid:
        raise HTTPException(422, f"signal must be one of {sorted(valid)}")
    if not (req.memory_id or req.pack_id):
        raise HTTPException(422, "pass memory_id or pack_id")

    with db.scoped(req.tenant_id, req.principal_id, req.project_id) as conn:
        fid = conn.execute(text(
            "INSERT INTO mem.feedback "
            "  (tenant_id, memory_id, pack_id, principal_id, signal, weight, note) "
            "VALUES (:t, :m, :p, :pr, :s, :w, :n) RETURNING id"),
            {"t": str(req.tenant_id), "m": str(req.memory_id) if req.memory_id else None,
             "p": req.pack_id, "pr": str(req.principal_id), "s": req.signal,
             "w": max(0.0, min(1.0, req.weight)), "n": req.note}).scalar_one()
    return {"id": str(fid), "signal": req.signal, "advisory": True}


class ScopeRequest(BaseModel):
    claims: dict


@app.post("/v1/scope/resolve")
def resolve_scope_endpoint(req: ScopeRequest) -> dict:
    """Turn verified token claims into a server-side scope (ADR-0004).

    The gateway verifies the signature; this resolves slugs to ids against the
    registry. Split that way because the gateway holds no database credentials
    and the API holds no JWKS — neither service can grant scope on its own.
    """
    from . import auth as _auth

    with db.engine().begin() as conn:
        try:
            scope = _auth.resolve_scope(conn, req.claims)
        except _auth.Forbidden as exc:
            raise HTTPException(403, str(exc)) from exc
    return scope.as_params() | {"org_slug": scope.org_slug,
                                "project_slug": scope.project_slug}


@app.get("/v1/admin/index-advice")
def index_advice_endpoint(tenant_id: UUID, project_id: UUID,
                          principal_id: UUID | None = None) -> dict:
    """Whether this tenant's corpus now warrants a partial HNSW index.

    Advisory by design: the API role holds no DDL rights (see
    maintenance.index_advice). This exists so the need is visible in a dashboard
    rather than only in a log line nobody is tailing.
    """
    from . import maintenance as _m

    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        slug = conn.execute(text(
            "SELECT o.slug FROM mem.organizations o WHERE o.id = :t"),
            {"t": str(tenant_id)}).scalar_one_or_none() or ""
        return _m.index_advice(conn, tenant_id=tenant_id, tenant_slug=slug)


@app.get("/v1/inbox")
def inbox_list(tenant_id: UUID, project_id: UUID,
               principal_id: UUID | None = None, limit: int = 50) -> dict:
    """The review queue (Phase 5, ADR-0015)."""
    from . import inbox as _inbox

    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        return _inbox.list_items(conn, tenant_id=tenant_id, project_id=project_id,
                                 limit=limit)


class ReviewRequest(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID
    ref: UUID
    action: str                 # promote | reject | resolve
    to_tier: str | None = None
    note: str = ""


@app.post("/v1/inbox/review")
def inbox_review(req: ReviewRequest) -> dict:
    """Act on one queued item. Every decision is audited."""
    from . import inbox as _inbox

    with db.scoped(req.tenant_id, req.principal_id, req.project_id) as conn:
        try:
            if req.action == "promote":
                return _inbox.promote(conn, tenant_id=req.tenant_id, memory_id=req.ref,
                                      to_tier=req.to_tier or "observed",
                                      reviewer=req.principal_id, note=req.note)
            if req.action == "reject":
                return _inbox.reject(conn, tenant_id=req.tenant_id, memory_id=req.ref,
                                     reviewer=req.principal_id, reason=req.note)
            if req.action == "resolve":
                return _inbox.resolve_conflict(
                    conn, tenant_id=req.tenant_id, conflict_id=req.ref,
                    resolution=req.note or "resolved", reviewer=req.principal_id)
            if req.action == "undo":
                return _inbox.unreview(conn, tenant_id=req.tenant_id,
                                       memory_id=req.ref, reviewer=req.principal_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
    raise HTTPException(422, "action must be promote, reject, resolve or undo")


@app.get("/v1/console/config")
def console_config() -> dict:
    """Bootstrap for the console: which scope to open on, if any.

    Returns the dev binding when one is configured, so `docker compose --profile
    console up` lands on a working screen instead of an empty form. Returns nulls
    otherwise and the console asks — it never guesses a tenant, because guessing
    one would mean showing an operator someone else's queue.
    """
    s = settings()
    return {
        "tenant_id": s.dev_tenant_id or None,
        "project_id": s.dev_project_id or None,
        "principal_id": s.dev_principal_id or None,
        "oauth": bool(s.oauth_issuer),
    }


@app.get("/v1/projects")
def list_projects(tenant_id: UUID, principal_id: UUID | None = None) -> dict:
    """Projects visible in this tenant — the console's project switcher.

    ONE SCOPED TRANSACTION PER PROJECT, deliberately. The obvious implementation
    counts every project's memories in one query with correlated subqueries, and
    it silently returns zero for all but the bound project: `mem.memories` is
    scoped by `allowed_projects()`, so a count taken under project A's binding
    cannot see project B's rows. That version was written, ran, and reported
    `active: 0` next to a project holding 23 memories.

    A wrong count in a switcher is worse than no count — it is the number a user
    would act on. So each project is counted inside its own binding, which is
    also the only way to be sure the caller may actually see it.
    """
    with db.scoped(tenant_id, principal_id or tenant_id, tenant_id) as conn:
        rows = conn.execute(text(
            "SELECT p.id, p.slug, p.name FROM mem.projects p "
            " WHERE p.tenant_id = :t ORDER BY p.slug"),
            {"t": str(tenant_id)}).mappings().all()

    out = []
    for r in rows:
        with db.scoped(tenant_id, principal_id or tenant_id, r["id"]) as conn:
            c = conn.execute(text(
                "SELECT count(*) FILTER (WHERE status = 'active')::int AS active, "
                "       count(*) FILTER (WHERE status = 'quarantined')::int AS quarantined "
                "  FROM mem.memories "
                " WHERE tenant_id = :t AND project_id = :p "
                "   AND upper(valid_at) IS NULL"),
                {"t": str(tenant_id), "p": str(r["id"])}).mappings().one()
        out.append({"id": str(r["id"]), "slug": r["slug"], "name": r["name"],
                    "active": c["active"], "quarantined": c["quarantined"]})
    return {"projects": out}


@app.get("/v1/health/project")
def project_health(tenant_id: UUID, project_id: UUID,
                   principal_id: UUID | None = None) -> dict:
    """Project health, with the formula returned alongside the score.

    03-FRONTEND §3.8: "`health` is a transparent composite ... and hovering shows
    the formula. An opaque health score is worse than none." So the components
    and their weights come back with the number — the console renders what the
    API computed rather than inventing a second definition that will drift.
    """
    from . import curation as _c

    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        counts = conn.execute(text(
            "SELECT "
            "  count(*) FILTER (WHERE status = 'active')::int AS active, "
            "  count(*) FILTER (WHERE status = 'quarantined')::int AS quarantined, "
            "  count(*) FILTER (WHERE status = 'archived')::int AS archived, "
            "  count(*) FILTER (WHERE status = 'active' "
            "        AND recorded_at < now() - interval '90 days')::int AS stale, "
            "  count(*) FILTER (WHERE status = 'active' "
            "        AND retrieval_count = 0)::int AS never_used "
            "  FROM mem.memories "
            " WHERE tenant_id = :t AND project_id = :p AND upper(valid_at) IS NULL"),
            {"t": str(tenant_id), "p": str(project_id)}).mappings().one()

        contested = conn.execute(text(
            "SELECT count(*)::int FROM mem.conflicts "
            " WHERE tenant_id = :t AND project_id = :p AND resolution IS NULL"),
            {"t": str(tenant_id), "p": str(project_id)}).scalar_one()

        top = conn.execute(text(
            "SELECT id, title, retrieval_count FROM mem.memories "
            " WHERE tenant_id = :t AND project_id = :p AND status = 'active' "
            "   AND retrieval_count > 0 "
            " ORDER BY retrieval_count DESC LIMIT 5"),
            {"t": str(tenant_id), "p": str(project_id)}).mappings().all()

        cur = _c.status(conn, tenant_id=tenant_id, project_id=project_id)

    active = max(counts["active"], 1)
    # Each component is a penalty in [0, 1], weighted, subtracted from 100. Named
    # and returned so the number can be argued with.
    parts = [
        ("contested", min(contested / 10.0, 1.0), 20),
        ("stale >90d", counts["stale"] / active, 20),
        ("never retrieved", counts["never_used"] / active, 15),
        ("review backlog", min(cur["inbox_depth"] / max(_c.DISABLE_DEPTH, 1), 1.0), 25),
    ]
    acc = cur["acceptance"]["rate"]
    if acc is not None:
        # Distance outside the 30-85% band, normalised. In band costs nothing.
        out = max(_c.ACCEPT_MIN - acc, acc - _c.ACCEPT_MAX, 0.0) / max(_c.ACCEPT_MIN, 0.01)
        parts.append(("acceptance outside band", min(out, 1.0), 20))

    score = round(100 - sum(p * w for _, p, w in parts))
    return {
        "health": max(score, 0),
        "formula": [{"component": n, "penalty": round(p, 3), "weight": w,
                     "cost": round(p * w, 1)} for n, p, w in parts],
        "counts": dict(counts) | {"contested": contested},
        "curation": cur,
        "top_retrieved": [{"ref": str(r["id"]), "title": r["title"],
                           "uses": r["retrieval_count"]} for r in top],
    }


@app.get("/v1/curation")
def curation_status(tenant_id: UUID, project_id: UUID,
                    principal_id: UUID | None = None) -> dict:
    """Curation health and the ADR-0015 kill-switch state.

    The dashboard endpoint from Phase 5's "curator instrumentation" line. It is
    read-only and reports rather than acts: the switch is consulted on the write
    path in extract.propose(), so nothing here can turn extraction on or off by
    being called, only describe what would happen if it were.
    """
    from . import curation as _c

    with db.scoped(tenant_id, principal_id or tenant_id, project_id) as conn:
        return _c.status(conn, tenant_id=tenant_id, project_id=project_id)


class ExtractRequest(BaseModel):
    tenant_id: UUID
    project_id: UUID
    principal_id: UUID
    text: str
    source_uri: str | None = None
    source_type: str = "session"
    dry_run: bool = False


@app.post("/v1/extract")
def extract_endpoint(req: ExtractRequest) -> dict:
    """Propose memories from a session transcript (Phase 5).

    Returns 200 with `enabled: false` when extraction is off, and 200 with
    `blocked: true` when the kill switch has fired. Neither is an error: both are
    normal, intended operating states, and returning 4xx for them would push
    callers into treating a working policy decision as a fault to retry around.
    """
    from . import extract as _x

    with db.scoped(req.tenant_id, req.principal_id, req.project_id) as conn:
        try:
            return _x.propose(
                conn, tenant_id=req.tenant_id, project_id=req.project_id,
                principal_id=req.principal_id, source_text=req.text,
                source_uri=req.source_uri, source_type=req.source_type,
                dry_run=req.dry_run)
        except _x.ExtractionUnavailable as exc:
            # The model is genuinely unreachable — that IS a fault, and it is
            # distinct from the two states above.
            raise HTTPException(503, str(exc)) from exc


@app.get("/v1/schema/objects")
def schema_objects() -> dict:
    """Phase 1 acceptance helper: confirms the migration actually created things,
    and that every table in mem has RLS enabled AND forced."""
    from sqlalchemy import text

    with db.engine().connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT c.relname,
                       c.relrowsecurity      AS rls_enabled,
                       c.relforcerowsecurity AS rls_forced
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'mem' AND c.relkind = 'r'
                 ORDER BY 1
                """
            )
        ).all()
    tables = [
        {"table": r[0], "rls_enabled": r[1], "rls_forced": r[2]} for r in rows
    ]
    unprotected = [t["table"] for t in tables if not (t["rls_enabled"] and t["rls_forced"])]
    return {"tables": tables, "unprotected": unprotected, "count": len(tables)}
