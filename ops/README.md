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

## Observability

| Surface | URL | What it answers |
|---|---|---|
| Grafana | http://localhost:3001 | Dashboards, logs (Explore → Loki), traces |
| Prometheus | http://localhost:9090 | `/targets` when a panel is empty, `/alerts` for the gates |
| Alertmanager | http://localhost:9093 | What is firing, grouping, silences |
| Tempo | http://localhost:3200 | Trace lookup by id (Grafana is the usual front end) |
| Loki | (internal `loki:3100`) | Log store; query it through Grafana Explore |

**Metrics, logs and traces are joined.** Application logs go out over OTLP with
the trace id attached, so a span links straight to the log lines written inside
it and back again. Postgres, PgBouncer and nginx will never speak OTLP; Promtail
ships their container output into the same store, which is what you actually want
at 3am. `{service="postgres"} |= "FATAL"` is one query.

**Alerting.** Prometheus evaluates, Alertmanager groups and routes. The stack
always exposes active alerts at `http://localhost:9093` and Grafana provisions
that Alertmanager as a datasource. To notify people, set `ALERT_WEBHOOK_URL` in
the untracked `.env` file to a relay that accepts the Alertmanager generic
webhook payload. That relay can forward to Slack, Teams, PagerDuty, email, or an
internal incident system. Leaving it blank is intentional for local development:
alerts remain visible in the UIs, but no notification destination is claimed.
The Operations dashboard shows both failed notification deliveries and the
Alertmanager config-reload result, so a configured route is observable rather
than assumed. Critical alerts route immediately; curation alerts (ADR-0015) are
on a daily cadence because that failure mode plays out over weeks and paging for
it at night trains people to ignore the channel.

**Dashboards.** *Memory — Retrieval & Context Packs* covers the serving path:
p95 pack latency against the 350 ms production gate, latency by stage, per-arm
retrieval contribution, trust tier of returned items, and why candidates were
dropped. *Memory — Curation & Trust* covers ADR-0015: review backlog against the
100/200 thresholds, the extraction kill switch, acceptance rate against the
30–85% band, and writes by assigned tier.

*Memory - Operations & Alerts* is the incident entry point: firing alerts,
every critical service contract, worker and scheduler loop liveness, database
backend health, scrape target state, and Alertmanager delivery are all on one
page. It links directly to Alertmanager, Prometheus targets, and the Knowledge
Console. All three dashboards are provisioned from
`ops/grafana/dashboards/`. Edit the JSON and Grafana picks it up within 30
seconds — no restart, no re-import.

**Where the metrics come from, and why it is split.** Request-time metrics come
from the API, recorded from work already done for a caller who proved their
scope. The backlog and curation gauges come from the **scheduler**, because they
are per-project aggregates: the API runs as `memory_app`, which is NOBYPASSRLS,
and a Prometheus scrape carries no scope. Giving the API a BYPASSRLS connection
so a dashboard could compute them would put a read-every-tenant role inside the
process that serves untrusted callers. `ops/prometheus.yml` therefore scrapes
both `api:8080` and `scheduler:9100`.

**Tracing.** `OTEL_EXPORTER_OTLP_ENDPOINT` alone emits nothing — the SDK has to
be installed and attached, which `memory_platform/telemetry.py` does. FastAPI,
httpx and psycopg are auto-instrumented. The embedder and cross-encoder call out
over `urllib`, which no auto-instrumentation sees, so they carry explicit spans
(`embed.http`, `rerank.http`); without them a 780 ms pack traces as 30 ms of SQL
and points you at the database, which is the one place the time is not going.

**When a panel is empty,** check in this order — it is almost always the first:

1. `http://localhost:9090/targets` — is the target UP?
2. Has any traffic happened? A *labelled* histogram exports no series at all
   until its first observation, so a metric can be legitimately absent rather
   than zero on a freshly restarted API.
3. `docker compose logs otel-collector` — is anything arriving?

`sh tests/run-all.sh` includes `test_observability`, which asserts delivered
data end to end rather than container health. It exists because the collector,
Tempo, Prometheus and Grafana all ran green for the entire build while the trace
pipeline carried zero bytes and Grafana had no dashboards at all.

## Production

```sh
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The overlay removes host port bindings, disables anonymous Grafana, and raises
pool and worker sizes. Anonymous Grafana in a shared environment exposes project
names, memory counts and query text — treat it as a data exposure, not a
convenience setting.
