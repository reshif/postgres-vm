"""Background worker — Phase 1 skeleton.

Connects DIRECT to Postgres. Procrastinate relies on LISTEN/NOTIFY, which is
session-scoped; behind transaction pooling the listener silently stops receiving
notifications and quietly degrades to polling. Do not point this at PgBouncer.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from procrastinate import App, PsycopgConnector
from sqlalchemy import text

from . import db, ingest, maintenance
from .config import settings

logging.basicConfig(level=settings().log_level.upper())
log = logging.getLogger("memory.worker")

# The worker and scheduler produce the logs an operator most often needs — poll
# ingestion, maintenance sweeps, the ADR-0015 kill switch — and neither serves
# HTTP, so nothing else here would ever have attached an exporter.
from . import telemetry as _telemetry  # noqa: E402
_telemetry.setup_logs()


def _dsn() -> str:
    # Procrastinate wants a plain libpq DSN, not the SQLAlchemy dialect prefix.
    url = settings().database_url_direct
    if "@pgbouncer" in url:
        raise SystemExit(
            "worker refuses to start against PgBouncer — LISTEN/NOTIFY does not "
            "survive transaction pooling. Use the direct URL."
        )
    return url.replace("postgresql+psycopg://", "postgresql://")


app = App(connector=PsycopgConnector(conninfo=_dsn()))


@app.task(queue="embedding", name="embed_memory")
async def embed_memory(tenant_id: str, project_id: str) -> None:
    """Backfill vectors for a project.

    Was a placeholder that logged and returned — so the queue existed and did
    nothing, and a memory written during an embedder outage kept its lexical
    searchability but never regained its vector.
    """
    from . import maintenance as _m

    def _run() -> dict:
        with db.scoped(UUID(tenant_id), UUID(tenant_id), UUID(project_id), direct=True) as conn:
            return _m.backfill_embeddings(conn, tenant_id=UUID(tenant_id),
                                          project_id=UUID(project_id))

    log.info("embed_memory backfill: %s", await asyncio.to_thread(_run))


@app.task(queue="ingestion", name="ingest_git_commit")
async def ingest_git_commit(project_id: str, sha: str) -> None:
    report = await asyncio.to_thread(_ingest_git_commit, project_id, sha)
    log.info("ingest_git_commit %s@%s: %s", project_id, sha[:12], report)


def _ingest_service_scope(project_id: UUID) -> tuple[UUID, UUID, str | None]:
    """Resolve one registered project and an attributable worker principal.

    The task receives an opaque project id from the queue, never a caller-supplied
    tenant id. Looking up its tenant before setting scope avoids turning a stale
    or malicious task payload into a cross-tenant write. The service principal
    is deterministic per tenant, so database audit/version triggers always have
    an actual actor rather than an invented UUID in their session GUC.
    """
    with db.engine_direct().begin() as conn:
        project = conn.execute(text(
            "SELECT tenant_id, repo_url FROM mem.projects WHERE id = :project"),
            {"project": str(project_id)}).mappings().one_or_none()
        if project is None:
            raise LookupError(f"project {project_id} is not registered")
        tenant_id = UUID(str(project["tenant_id"]))
        principal_id = uuid5(NAMESPACE_URL, f"memory-platform:ingest:{tenant_id}")
        principal_id = conn.execute(text(
            "INSERT INTO mem.principals "
            "  (id, tenant_id, actor, external_id, display_name) "
            "VALUES (:id, :tenant, 'service', :external, 'Plane A ingestion worker') "
            "ON CONFLICT (tenant_id, actor, external_id) DO UPDATE "
            "  SET display_name = EXCLUDED.display_name "
            "RETURNING id"),
            {"id": str(principal_id), "tenant": str(tenant_id),
             "external": "plane-a-ingestion"}).scalar_one()
    return tenant_id, UUID(str(principal_id)), project["repo_url"]


def _normalise_repo_url(url: str) -> str:
    """Match the CLI binding's SSH/HTTPS-insensitive remote identity."""
    import re

    value = (url or "").strip().lower().rstrip("/")
    value = re.sub(r"^(https?://|git\+ssh://|ssh://)", "", value)
    value = re.sub(r"^git@", "", value)
    if value.startswith(("github.com", "gitlab.com", "bitbucket.org")):
        value = value.replace(":", "/", 1)
    return re.sub(r"\.git$", "", value)


def _assert_checkout_binding(repo_root: Path, registered_remote: str | None) -> None:
    """Refuse to process a queue item using a checkout bound to another project."""
    if not registered_remote:
        return
    try:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo_root}", "-C", str(repo_root),
             "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=15, check=False)
    except OSError as exc:
        raise RuntimeError(f"cannot inspect checkout remote: {exc}") from exc
    actual = result.stdout.strip() if result.returncode == 0 else ""
    if not actual or _normalise_repo_url(actual) != _normalise_repo_url(registered_remote):
        raise PermissionError(
            "checkout remote does not match the project registration; refusing "
            f"to ingest {repo_root} for this task")


def _ingest_git_commit(
    project_id: str,
    sha: str,
    *,
    repo_root: Path | None = None,
) -> dict:
    """Ingest the `.memory` tree as it existed at exactly ``sha``.

    This synchronous core is intentionally separate from the Procrastinate
    wrapper so it can be exercised against a temporary repository. It does not
    alter the checkout and it does not use the scheduler's dev scope binding.
    """
    project = UUID(project_id)
    root = (repo_root or Path(settings().ingest_repo_path)).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"ingest checkout does not exist: {root}")
    tenant, principal, registered_remote = _ingest_service_scope(project)
    _assert_checkout_binding(root, registered_remote)
    with ingest.commit_snapshot(root, sha) as snapshot:
        with db.scoped(tenant, principal, project, direct=True) as conn:
            report = ingest.ingest_tree(
                conn, snapshot, tenant_id=tenant, project_id=project,
                principal_id=principal, provenance_repo=root, source_ref=sha)
    return {
        **report.summary(), "created_files": report.created,
        "archived_keys": report.archived,
        "rejected": [{"path": path, "reason": reason}
                     for path, reason in report.rejected],
    }


async def _run_worker() -> None:
    from . import metrics as _metrics

    queues = [q.strip() for q in settings().worker_queues.split(",") if q.strip()]
    log.info("worker starting on queues: %s", queues)
    _metrics.serve(int(os.environ.get("MEMORY_METRICS_PORT", "9101")))
    pulse = asyncio.create_task(_heartbeat_loop("worker"))
    try:
        async with app.open_async():
            await app.run_worker_async(queues=queues)
    finally:
        pulse.cancel()
        try:
            await pulse
        except asyncio.CancelledError:
            pass


async def _heartbeat_loop(service: str) -> None:
    """Keep a freshness signal moving while the service event loop is healthy."""
    from . import metrics as _metrics

    while True:
        _metrics.heartbeat(service)
        await asyncio.sleep(15)


def _tree_fingerprint(root: Path) -> str:
    """Cheap change detector for the .memory/ tree.

    Path, size and mtime of every file. Deliberately not a content hash: this runs
    every interval and hashing the tree to discover that nothing changed is the
    wrong shape of work. ingest_tree is idempotent anyway, so this is an
    optimisation — a false "unchanged" costs freshness, never correctness, and a
    false "changed" costs one wasted reconcile.
    """
    if not root.is_dir():
        return ""
    parts = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            parts.append(f"{p.as_posix()}:{st.st_size}:{int(st.st_mtime)}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _ensure_bound_scope() -> None:
    """Make sure the bound org/project/principal exist before ingesting into them.

    mem.memories.project_id is a foreign key, so ingesting into a project nobody
    registered fails with an IntegrityError — once per poll interval, forever,
    while the scheduler reports itself healthy and the memory stays empty. On a
    fresh database that is the default state, because until now the only thing
    that created these rows was the eval harness.

    Registering a project properly is `memory init` (05-BUILD-PLAN Phase 2). This
    is the dev-binding equivalent: idempotent, explicitly logged, and only ever
    touching the ids the operator already named in MEMORY_DEV_*. It creates a
    container to put memories in — it does not invent a tenant that was not asked
    for.
    """
    s = settings()
    tenant, project = UUID(s.dev_tenant_id), UUID(s.dev_project_id)
    principal = UUID(s.dev_principal_id) if s.dev_principal_id else None

    with db.engine().begin() as c:
        existing = c.execute(
            text("SELECT count(*) FROM mem.projects WHERE id = :p"), {"p": str(project)}
        ).scalar_one()
        if existing:
            return

        log.warning(
            "bound project %s is not registered; creating it from the dev binding. "
            "This is what `memory init` will do properly in Phase 2.", project)
        c.execute(text("INSERT INTO mem.organizations (id, slug, name) "
                       "VALUES (:i, :s, :s) ON CONFLICT DO NOTHING"),
                  {"i": str(tenant), "s": f"tenant-{str(tenant)[:8]}"})
        c.execute(text("INSERT INTO mem.projects (id, tenant_id, slug, name) "
                       "VALUES (:i, :t, :s, :s) ON CONFLICT DO NOTHING"),
                  {"i": str(project), "t": str(tenant), "s": f"project-{str(project)[:8]}"})
        if principal:
            c.execute(text("INSERT INTO mem.principals "
                           "  (id, tenant_id, actor, external_id, display_name) "
                           "VALUES (:i, :t, 'service', :e, 'plane-a-ingest') "
                           "ON CONFLICT DO NOTHING"),
                      {"i": str(principal), "t": str(tenant), "e": f"ingest-{principal}"})


def _poll_once(last: str) -> str:
    """One reconcile pass. Returns the new fingerprint."""
    s = settings()
    root = Path(s.ingest_repo_path)
    fp = _tree_fingerprint(root / ".memory")
    if not fp:
        log.warning("no .memory/ tree under %s — nothing to ingest", root)
        return fp
    if fp == last:
        return fp

    tenant = UUID(s.dev_tenant_id)
    project = UUID(s.dev_project_id)
    principal = UUID(s.dev_principal_id) if s.dev_principal_id else None

    with db.scoped(tenant, principal or tenant, project, direct=True) as conn:
        report = ingest.ingest_tree(conn, root, tenant_id=tenant,
                                    project_id=project, principal_id=principal)
    summary = report.summary()
    if any(summary[k] for k in ("created", "archived", "rejected")):
        log.info("plane A reconciled: %s", summary)

    # Maintenance runs after reconciliation, in its own transaction: conflict
    # detection should see the documents this pass just ingested, and a failure
    # in a sweep must not roll back the ingestion that succeeded.
    with db.scoped(tenant, principal or tenant, project, direct=True) as conn:
        stats = maintenance.run_all(conn, tenant_id=tenant, project_id=project)
    if any(v for k, v in stats.items() if v and v != {"archived": 0}):
        log.info("maintenance: %s", stats)
    if report.rejected:
        # Loud on purpose. A file silently missing from the index because a
        # scanner rejected it is the kind of thing discovered months later while
        # debugging something else.
        for path, why in report.rejected:
            log.error("ingest REJECTED %s: %s", path, why)
    return fp


def _curation_sample() -> dict:
    """Sample inbox depth for the bound project.

    Deliberately NOT folded into _poll_once. That function returns early when the
    repository fingerprint is unchanged, so nothing after it runs on a quiet
    repo — and a quiet repo is precisely the state of a project whose inbox has
    been abandoned. Hanging the ADR-0015 kill switch off tree changes would mean
    the switch collects no evidence in exactly the case it exists to catch.
    """
    from . import curation, metrics

    s = settings()
    tenant = UUID(s.dev_tenant_id)
    project = UUID(s.dev_project_id)
    principal = UUID(s.dev_principal_id) if s.dev_principal_id else None
    with db.scoped(tenant, principal or tenant, project, direct=True) as conn:
        stats = curation.snapshot(conn, tenant_id=tenant, project_id=project)

        # Gauges are published from here rather than from the API because these
        # are aggregates across a project, and the API runs as memory_app —
        # NOBYPASSRLS, with no scope on a Prometheus scrape. It cannot compute
        # them, and handing it a BYPASSRLS connection to make a dashboard work
        # would put a read-every-tenant role inside the process that serves
        # untrusted callers.
        status = curation.status(conn, tenant_id=tenant, project_id=project)
        counts = dict(conn.execute(text(
            "SELECT status::text, count(*)::int FROM mem.memories "
            " WHERE tenant_id = :t AND project_id = :p AND upper(valid_at) IS NULL "
            " GROUP BY 1"), {"t": str(tenant), "p": str(project)}).all())
        open_conflicts = conn.execute(text(
            "SELECT count(*)::int FROM mem.conflicts "
            " WHERE tenant_id = :t AND project_id = :p AND resolution IS NULL"),
            {"t": str(tenant), "p": str(project)}).scalar_one()

        # ADR-0008's keep-or-cut rule needs a month of evidence, so the figure has
        # to accumulate continuously. Sampled here, with the other aggregates,
        # for the same NOBYPASSRLS reason.
        from . import arms as _arms
        arm_report = _arms.contribution(conn, tenant_id=tenant, project_id=project)

    slug = s.dev_project_id[:8]
    metrics.publish_curation(slug, status, counts)
    metrics.publish_conflicts(slug, open_conflicts)
    metrics.publish_arm_contribution(slug, arm_report)
    return stats


async def _run_scheduler() -> None:
    """Plane A poll ingestion, plus the consolidation jobs that land in Phase 7.

    Ingestion runs here rather than in a worker task because it is reconciliation,
    not queued work: there is no event to consume, and two concurrent workers
    reconciling the same tree would race on the supersede path.

    Poll rather than webhook-only. A webhook that is missed leaves the platform
    permanently stale with no signal that it happened, and "the memory silently
    stopped updating" is the one failure a memory system cannot afford. The
    webhook endpoint (POST /v1/ingest) exists for latency; this loop exists for
    correctness.
    """
    from . import metrics as _metrics

    s = settings()
    # Both enabled and intentionally-idle schedulers need a scrapeable liveness
    # signal. Starting this after the feature guard made the target look down
    # whenever poll ingestion was disabled.
    _metrics.serve(int(os.environ.get("MEMORY_METRICS_PORT", "9100")))
    if not (s.ingest_enabled and s.dev_tenant_id and s.dev_project_id):
        log.info("scheduler: poll ingestion disabled "
                 "(set MEMORY_INGEST_ENABLED and the scope binding to turn it on)")
        while True:
            _metrics.heartbeat("scheduler")
            await asyncio.sleep(3600)

    try:
        await asyncio.to_thread(_ensure_bound_scope)
    except Exception as exc:  # noqa: BLE001
        log.exception("could not verify the bound project, ingestion will retry: %s", exc)

    log.info("scheduler: polling %s/.memory every %ss",
             s.ingest_repo_path, s.ingest_interval_s)
    fingerprint = ""
    ticks = 0
    # One curation sample per hour of wall clock, upserted into the day's row.
    # The table's grain is daily; sampling hourly just means the day's figure
    # reflects the queue as it was late in the day rather than at midnight.
    sample_every = max(1, int(3600 / max(s.ingest_interval_s, 1)))
    while True:
        _metrics.heartbeat("scheduler")
        try:
            fingerprint = await asyncio.to_thread(_poll_once, fingerprint)
        except Exception as exc:  # noqa: BLE001
            # Never let one bad pass kill the loop: a scheduler that exits on a
            # transient database blip stops ingesting forever and looks healthy.
            log.exception("ingest pass failed, retrying next interval: %s", exc)

        if ticks % sample_every == 0:
            try:
                stats = await asyncio.to_thread(_curation_sample)
                if stats["inbox_depth"]:
                    log.info("curation: %s", stats)
            except Exception as exc:  # noqa: BLE001
                log.exception("curation sample failed: %s", exc)
        ticks += 1
        await asyncio.sleep(s.ingest_interval_s)


def main() -> None:
    role = os.environ.get("MEMORY_ROLE", "worker")
    asyncio.run(_run_scheduler() if role == "scheduler" else _run_worker())


if __name__ == "__main__":
    main()
