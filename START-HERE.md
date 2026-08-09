# Start here

Everything needed to begin Phase 0 and Phase 1. Nothing to combine — this is the
whole package.

## What is in the box

```
memory-platform-blueprint/
├── START-HERE.md                    ← you are here
├── README.md                        package index + the five takeaways
├── 00-MASTER-BLUEPRINT.md           the full argument (read end to end, once)
├── 01-SCHEMA.sql                    Phase 1-4 schema: bi-temporal, RLS FORCE,
│                                    embeddings, graph, hybrid search, pgTAP gate
├── 02-MCP-CONTRACT.md               four-tool surface, spec 2026-07-28 conformance
├── 03-FRONTEND-KNOWLEDGE-CONSOLE.md the knowledge frontend, screen by screen
├── 04-EVALUATION.md                 seven suites, five arms, both go/no-go gates
├── 05-BUILD-PLAN.md                 nine phases, one binary acceptance test each
├── docker-compose.yml               local-first stack
├── docker-compose.prod.yml          overlay for anything that is not a laptop
├── .env.example                     copy to .env, change the four passwords
├── .gitignore
├── ops/
│   ├── README.md                    connection topology, verification commands
│   ├── initdb/01-roles.sql          roles (NOBYPASSRLS) + pgbouncer auth_query
│   └── pgbouncer/gen-userlist.sh    run once before first start
└── .memory/
    └── decisions/                   ADR-0001..0015 — copy straight into your repo
        └── README.md                index + what changed from the draft set
```

The `.memory/decisions/` folder is already in the right shape: copy the whole
`.memory/` directory to the root of the memory-platform repository and it becomes
your Plane A seed content. It is also the Phase 4 acceptance test — the system
must be able to answer "why did we reject a separate graph database?" from it.

## Day one, in order

```sh
# 1. Seed Plane A
cp -r .memory /path/to/memory-platform-repo/
cd /path/to/memory-platform-repo && git add .memory && git commit -m "ADR-0001..0015"

# 2. Bring up the stack
cp .env.example .env          # change the four passwords
sh ops/pgbouncer/gen-userlist.sh
docker compose up -d
docker compose logs -f embeddings     # first run downloads BGE-M3; minutes

# 3. Verify isolation BEFORE writing any application code
docker compose exec postgres psql -U memory_app -d memory \
  -c "SELECT count(*) FROM mem.memories;"
# expect 0 — no scope context set, so RLS returns nothing.
# If this returns rows, stop and audit. Nothing else matters until it returns 0.

# 4. Verify the pool is actually in the path
docker compose exec pgbouncer \
  psql -h 127.0.0.1 -p 6432 -U pgbouncer_auth -d pgbouncer -c 'SHOW POOLS'
# cl_active on the `memory` pool should be non-zero once the API serves traffic.
```

## The one decision that blocks Phase 1

**Pin your Postgres version.** It determines which temporal branch you build on
and it cannot be deferred:

- **PG18+** — use `UNIQUE (..., valid_at WITHOUT OVERLAPS)` as written in
  `01-SCHEMA.sql`. This is what the pinned image `pgvector/pgvector:0.8.2-pg18`
  gives you.
- **PG < 18** — use the ADR-0006 portability branch in the same file
  (`EXCLUDE USING gist`). Same model, different mechanism, documented migration
  path. Run the temporal test suite against both branches until every deployment
  is on 18.

## The second decision, before Phase 3

Extraction mode (ADR-0015). The plan assumes **(a) deterministic-only until the
Review Inbox lands in Phase 5**, which makes an unattended quarantine queue
structurally impossible. `MEMORY_LLM_PROVIDER=none` in `.env.example` enforces it.
If you want LLM extraction earlier, move the Inbox to Phase 2 — do not enable
extraction without it.

## What "done" means at each milestone

| Phase | The demo |
|---|---|
| 1 | "Project B literally cannot see project A's data — here's the SQL proving it" |
| 2 | "I merged an ADR; 30 seconds later the agent can cite it, with a link to the commit" |
| 3 | "Here is exactly why this memory ranked first, and what we dropped and why" |
| 4 | "Cursor just used a decision Claude recorded yesterday, in a different session" |
| 5 | "Five minutes of review a week keeps the whole thing clean" |
| 6 | "Agents ask 40% fewer questions we've already answered — against the honest baseline" |

## The gate that matters

Phase 6 (`04-EVALUATION.md` §7.2). Arm D beats arm B — filesystem + grep +
`AGENTS.md` — on at least 3 of 5 headline metrics, or you stop and ship the
two-plane git convention alone. That is a real product and a legitimate outcome.
Discovering it in month four is a success.

Build the baseline arm in Phase 3, not Phase 6. A gate you cannot measure until
the day you need it is not a gate.
