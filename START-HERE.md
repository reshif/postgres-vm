# Start here

Runnable memory-platform implementation. The foundation, ingestion, retrieval,
MCP surface, curation console, and selected structure and hardening features are
implemented; Phase 3 latency and the later roadmap acceptance gates
are still open.

## Quick start

```sh
cp .env.example .env                  # change the four passwords
sh ops/pgbouncer/gen-userlist.sh      # or see "Windows" below
ollama pull bge-m3                    # host embedding model; first run is ~1.2 GB
# Ollama must listen beyond localhost for containers to reach it:
# OLLAMA_HOST=0.0.0.0:11434 ollama serve
docker compose up -d --wait
sh tests/run-all.sh
```

**If you ran an earlier version of this package**, the Postgres volume was created
at the pre-PG18 path and the container will restart-loop. Wipe it and start clean:

```sh
docker compose down -v
docker compose up -d
```

`-v` destroys the database. That is fine here — there is nothing in it yet, and
Plane A lives in git precisely so the database is always disposable.

Then the two checks that matter:

```sh
# 1. Isolation. This is the most important assertion in the system.
docker compose exec postgres psql -U memory_app -d memory \
  -c "SELECT count(*) FROM mem.memories;"
# expect 0 — no scope context set, so RLS returns nothing.
# If it returns rows, stop and audit. Nothing else matters until it returns 0.

# 2. Readiness, including that same check run through the pooled path.
curl -s localhost:8080/readyz | python -m json.tool

# 3. Every table has RLS enabled AND forced.
curl -s localhost:8080/v1/schema/objects | python -m json.tool
# "unprotected" must be an empty list.
```

### Windows / PowerShell

`gen-userlist.sh` needs a POSIX shell. Either run it in Git Bash / WSL, or
generate the file directly:

```powershell
docker compose run --rm --entrypoint python init `
  -c "import base64,hashlib,hmac,os;p=b'change-me-pgb';s=os.urandom(16);i=4096;k=hashlib.pbkdf2_hmac('sha256',p,s,i);ck=hmac.new(k,b'Client Key',hashlib.sha256).digest();sk=hashlib.sha256(ck).digest();srv=hmac.new(k,b'Server Key',hashlib.sha256).digest();print('\"pgbouncer_auth\" \"SCRAM-SHA-256\${0}:{1}\${2}:{3}\"'.format(i,base64.b64encode(s).decode(),base64.b64encode(sk).decode(),base64.b64encode(srv).decode()))" `
  | Out-File -Encoding ascii ops/pgbouncer/userlist.txt
```

Use the same password you put in `DB_PGBOUNCER_PASSWORD`. Also make sure Git does
not rewrite line endings in the shell script and SQL files:
`git config core.autocrlf input`.

## What is in the box

```
memory-platform-blueprint/
├── START-HERE.md                    ← you are here
├── README.md                        package index
├── 00-MASTER-BLUEPRINT.md           the full argument (read end to end, once)
├── 01-SCHEMA.sql                    the schema; executed verbatim by migration 0001
├── 02-MCP-CONTRACT.md               four-tool surface, spec 2026-07-28
├── 03-FRONTEND-KNOWLEDGE-CONSOLE.md the console, screen by screen (Phase 5)
├── 04-EVALUATION.md                 seven suites, five arms, both go/no-go gates
├── 05-BUILD-PLAN.md                 nine phases, one binary acceptance test each
│
├── docker-compose.yml               local stack
├── docker-compose.prod.yml          overlay for anything that is not a laptop
├── .env.example  .gitignore
│
├── pyproject.toml                   dependencies
├── alembic.ini
├── migrations/
│   ├── env.py                       refuses to migrate through PgBouncer
│   └── versions/0001_initial_schema.py
├── src/memory_platform/
│   ├── config.py                    settings
│   ├── db.py                        engines + the ONLY scoped-transaction helper
│   ├── api.py                       /healthz /readyz /v1/schema/objects
│   ├── mcp_server.py                server/discover + tools/list, spec 2026-07-28
│   └── worker.py                    procrastinate app; refuses to run via PgBouncer
│
├── ops/
│   ├── Dockerfile                   targets: api, mcp, worker
│   ├── README.md                    connection topology + verification commands
│   ├── initdb/01-roles.sql          roles (NOBYPASSRLS) + pgbouncer auth_query
│   ├── pgbouncer/pgbouncer.ini      full config, image-independent
│   ├── pgbouncer/gen-userlist.sh    run once before first start
│   ├── otel-config.yaml  tempo.yaml  prometheus.yml
│   └── grafana/datasources/datasources.yml
│
├── console/README.md                Phase 5; the service is profile-gated
└── .memory/decisions/               ADR-0001..0015 — copy into your repo
```

## Current implementation status

Honest inventory, so you know what you are looking at:

| Component | State |
|---|---|
| Schema, RLS, temporal constraints, hybrid-search function | **Real.** Applied through migration 0018. |
| Roles, PgBouncer auth_query, connection topology | **Real.** |
| `db.scoped()` — the only sanctioned transaction path | **Real.** No unscoped equivalent is exposed. |
| `/readyz`, `/v1/schema/objects` | **Real.** They run the isolation self-test. |
| CLI, project binding, deterministic capture, Plane A ingestion | **Real.** Covered by API and real-Git-workspace CLI acceptance tests. |
| Context engine, hybrid retrieval, ranking, explainability | **Real.** Suite 1 quality gates pass on the pinned corpus; Phase 3 remains unaccepted because the measured p95 exceeds the roadmap's `<300 ms` latency target. |
| MCP tools and resources | **Real.** Four tools (context, search, write, explain) plus RLS-scoped project, conflict, memory, and entity resources. The automated suite verifies two independent MCP sessions, event provenance, private resource caching, and bound-scope isolation; the manual cross-client acceptance demo remains to be run. |
| Workers | **Real.** Poll ingestion, embedding, extraction and maintenance paths are implemented. |
| Console | **Built for the specified curation and inspection views.** Inbox, Explorer bulk lifecycle actions, tabbed memory evidence, procedures, graph with table fallback, bi-temporal timeline, conflicts, debugger, health, evaluation history, settings/grants, audit, and project administration run through the scoped API. Privileged configuration mutation remains an explicit control-plane boundary. |

The detailed roadmap and binary acceptance gates remain in `05-BUILD-PLAN.md`.
Do not treat a feature being present as a phase being accepted: the evaluation
and cross-client milestones are the deciding evidence.

## Seed Plane A

```sh
cp -r .memory /path/to/memory-platform-repo/
cd /path/to/memory-platform-repo && git add .memory && git commit -m "seed project memory"
```

That folder is your Phase 0 deliverable, your authoritative knowledge seed, and
part of the Phase 4 acceptance test: the system must answer "why did we reject a
separate graph database?" from it in a separately bound agent session.

## The decision that blocks Phase 1

**Postgres version.** The pinned image `pgvector/pgvector:0.8.2-pg18` gives you
PG18, so `UNIQUE (..., valid_at WITHOUT OVERLAPS)` works as written. If you must
run below 18, switch to the ADR-0006 portability branch in `01-SCHEMA.sql`
(`EXCLUDE USING gist`) — same model, different mechanism, documented migration
path — and run the temporal tests against both branches until everything is on 18.

## The gate that matters

Phase 6, `04-EVALUATION.md` §7.2. Arm D beats arm B — filesystem + grep +
`AGENTS.md` — on at least 3 of 5 headline metrics, or you stop and ship the
two-plane git convention alone. Build that baseline arm in Phase 3, not Phase 6.
A gate you cannot measure until the day you need it is not a gate.

## Current evaluation standing

Suite 1 runs against `eval/corpus/.memory`, never the live project tree. The
current quality baseline passes: `recall@5 = 0.900`, `MRR = 0.794`,
`nDCG@10 = 0.815`, and `forbidden@10 = 0`. It is not a Phase 3 acceptance yet:
the latest p95 search measurement is `548 ms`, above the roadmap's `<300 ms`
target. The evaluator now records that as a failed gate and exits non-zero.
Historical results are recorded in `eval/RESULTS.md`; compare only runs with the
same snapshot id and configuration.
