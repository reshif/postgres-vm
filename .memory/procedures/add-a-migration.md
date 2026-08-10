---
id: PROC-0003
title: Add a database migration
status: active
date: 2026-08-10
---

# Add a database migration

## Steps

1. Create `migrations/versions/NNNN_short_name.py` with `down_revision` pointing
   at the current head.
2. Execute SQL through the raw psycopg cursor:
   `with op.get_bind().connection.cursor() as cur: cur.execute(SQL)`.
   Do not use `exec_driver_sql` for multi-statement scripts.
3. Write a single `%` for the pg_trgm similarity operator, never `%%`.
4. Apply with `docker compose up -d --build init`, then confirm:
   `SELECT version_num FROM public.alembic_version`.

## Rules

- Migrations run as `memory_owner`. `memory_app` holds only CONNECT plus the
  grants the migrations issue, and cannot create schemas or grant to itself.
- A new tenant-scoped table needs RLS, FORCE and policies in the same migration.
  `tests/test_rls_coverage.py` fails the build otherwise, by design.
- Replacing a SQL function with a new signature: drop the old overload in the
  same migration. Leaving it lets existing call sites silently resolve to the
  version without the new behaviour.
- Never edit a ranking profile in place. Insert a new version and deactivate the
  old one.

## Verification

Run `sh tests/run-all.sh`. For anything touching policies or grants, confirm
`test_rls_coverage` specifically, since it is the suite that checks the property
rather than an example of it.
