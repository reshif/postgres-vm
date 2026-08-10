# ops/ — running the stack

## First run

```sh
cp .env.example .env          # then edit the four passwords
sh ops/pgbouncer/gen-userlist.sh
docker compose up -d
docker compose logs -f embeddings     # first run downloads BGE-M3; takes minutes
```

`init` runs Alembic and exits; `api` waits for it to complete successfully, so
`docker compose up` is one command with no manual migration step.

## .env.example

```sh
DB_OWNER_PASSWORD=change-me-owner
DB_APP_PASSWORD=change-me-app
DB_RO_PASSWORD=change-me-ro
DB_PGBOUNCER_PASSWORD=change-me-pgb

MEMORY_LLM_PROVIDER=none        # none | anthropic | openai | ollama
MEMORY_LLM_MODEL=
MEMORY_LOG_LEVEL=info

MCP_OAUTH_ISSUER=
MCP_OAUTH_AUDIENCE=https://memory.local/mcp
```

Add to `.gitignore`:

```
.env
ops/pgbouncer/userlist.txt
```

## Connection topology — the thing to internalise

```
api       ──► pgbouncer:6432 (transaction pooling) ──► postgres:5432
worker    ──► postgres:5432  DIRECT
scheduler ──► postgres:5432  DIRECT
init      ──► postgres:5432  DIRECT
```

Only the API is pooled. The reasons are specific, and each one has cost somebody
a debugging afternoon:

| Service | Why direct |
|---|---|
| `worker` | Procrastinate uses `LISTEN`/`NOTIFY`. Notifications are session-scoped; transaction pooling reassigns the connection between transactions and the listener silently stops receiving them. Symptom: the queue "just seems slow" because it has quietly fallen back to polling. |
| `scheduler` | Holds long consolidation transactions that would occupy pool slots the API needs. |
| `init` | Alembic takes session-level advisory locks and runs DDL; neither is safe under transaction pooling. |

The API being pooled is also *why* `mem.fn_set_scope` uses `set_config(..., true)`
rather than a bare `SET`. On a shared connection, a session-scoped setting outlives
the request and the next tenant inherits it. That is the leak the whole isolation
model exists to prevent, arriving through the back door.

## Verifying the pool is actually in the path

A pooler that is running but bypassed is worse than no pooler, because it looks
like coverage you do not have. Check:

```sh
docker compose exec pgbouncer \
  psql -h 127.0.0.1 -p 6432 -U pgbouncer_auth -d pgbouncer -c 'SHOW POOLS'
```

While the API is serving traffic, `cl_active` on the `memory` pool should be
non-zero. If it stays at zero, something is connecting around the pooler — check
`MEMORY_DATABASE_URL` in the `api` service.

Also confirm the split is real:

```sh
docker compose exec postgres psql -U memory_owner -d memory -c \
  "SELECT application_name, count(*) FROM pg_stat_activity
    WHERE datname='memory' GROUP BY 1 ORDER BY 2 DESC;"
```

You should see worker/scheduler connections alongside pooled API connections.

## Postgres restart-loops on first start

Symptom, repeating in `docker compose logs postgres`:

```
Error: in 18+, these Docker images are configured to store database data in a
       format which is compatible with "pg_ctlcluster" ...
       Counter to that, there appears to be PostgreSQL data in:
         /var/lib/postgresql/data (unused mount/volume)
```

Cause: PG18+ images moved the data directory into a major-version subdirectory
(`/var/lib/postgresql/18/docker`) so `pg_upgrade --link` works without crossing a
mount boundary. The volume must be mounted at `/var/lib/postgresql`, not at
`/var/lib/postgresql/data` (docker-library/postgres#1259).

The compose file mounts the correct path. If you created the volume with an
earlier version, remove it:

```sh
docker compose down -v && docker compose up -d
```

## Verifying isolation actually holds

The single most important check in the stack, and it should be in CI, not a
runbook. Locally:

```sh
docker compose exec postgres psql -U memory_app -d memory -c \
  "SELECT count(*) FROM mem.memories;"
# expect 0 — no scope context has been set, so RLS returns nothing
```

If that returns rows, stop and audit before writing another line of code.

## Prepared statements and PgBouncer

psycopg3 uses named prepared statements by default; those do not survive
transaction pooling unless the pooler manages them. Two settings work together:

- PgBouncer: `MAX_PREPARED_STATEMENTS=200`
- App: `MEMORY_DB_PREPARE_THRESHOLD=0` (disables client-side named statements)

If you see `prepared statement "_pg3_N" already exists`, that pair is the knob.
Belt and braces is deliberate here — the failure only appears under concurrency,
which is exactly when you least want to debug it.

## Embeddings has no healthcheck, on purpose

The TEI image ships without `curl` or `wget`, so the obvious healthcheck fails
permanently and drags every dependent service down with it. The API tolerates a
cold embedder by design (retrieval degrades to the lexical arm rather than
failing closed), so `service_started` is the correct dependency.

Probe it by hand when you need to:

```sh
curl -s localhost:8090/health
curl -s localhost:8090/embed -H 'content-type: application/json' \
  -d '{"inputs":"hello"}' | head -c 120
```

## Scaling workers

`deploy.replicas` is Swarm syntax and is silently ignored by `docker compose up`.
Scale explicitly:

```sh
docker compose up -d --scale worker=2
```

## Production

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The overlay removes host port bindings, disables anonymous Grafana, and raises
pool and worker sizes. Anonymous Grafana in a shared environment exposes project
names, memory counts and query text — treat it as a data exposure, not a
convenience setting.
