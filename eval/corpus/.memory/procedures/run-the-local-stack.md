---
id: PROC-0001
title: Run the local stack from a clean checkout
status: active
date: 2026-08-10
---

# Run the local stack from a clean checkout

## Preconditions

- Docker Desktop running, with at least 12 GB available to the WSL2 VM
- Ollama installed on the host, listening on `0.0.0.0:11434`, with `bge-m3` pulled

## Steps

1. `cp .env.example .env` and change every password.
2. `sh ops/pgbouncer/gen-userlist.sh` — writes `ops/pgbouncer/userlist.txt`.
   Run this before the first start, and again whenever you rotate
   `DB_PGBOUNCER_PASSWORD`.
3. `ollama pull bge-m3` if you have not already. It is 1.2 GB and 1024-dim.
4. `docker compose up -d --wait`. Migrations run automatically in the `init`
   service, which exits 0 when finished.

## Verification

- `curl -s localhost:8080/readyz` returns `"ready":true` with database, isolation
  and embeddings all ok.
- `docker compose ps` shows `init` as `Exited (0)` — that is success, not failure.
- `SHOW POOLS` on the pooler shows non-zero `cl_active` on the `memory` pool while
  the API serves traffic. If it stays at zero, something is bypassing PgBouncer.

## Failure modes

- **Embeddings show `degraded`.** The embedder is cold or unreachable. Retrieval
  falls back to the lexical arm; this is by design and does not fail readiness.
- **`init` exits non-zero.** Read `docker compose logs init`. It is almost always
  a migration error, not a connectivity error.
- **PgBouncer reports `server login failed: wrong password type`.** `userlist.txt`
  contains a SCRAM verifier instead of a plaintext password. See the troubleshooting
  procedure.
