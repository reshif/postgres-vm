---
id: conventions
title: Team conventions for the memory platform
status: active
date: 2026-08-10
---

# Conventions

## Database access

Open every transaction with `db.scoped(tenant_id, principal_id, project_id)`.
There is deliberately no unscoped equivalent for application code. If you need to
read without a scope you are writing an admin tool, and you should say so.

Never test isolation as `memory_owner`. It is the image's `POSTGRES_USER` and
therefore a superuser, which bypasses RLS including FORCE. Isolation tests connect
as `memory_app` through PgBouncer, because that is the path production uses.

## Migrations

Migrations run as `memory_owner`, because they create the schema and issue grants
to `memory_app` — a role cannot grant privileges to itself.

Execute multi-statement SQL through the raw psycopg cursor, not
`exec_driver_sql`. psycopg only skips placeholder parsing when params is `None`,
and SQLAlchemy passes an empty tuple, so any literal `%` in reviewed SQL raises
`incomplete placeholder`. For the same reason, write a single `%` in migration
SQL, never `%%`.

When you add a table with a `tenant_id`, add its RLS policies in the same
migration. When you add a serial column, the default privileges from migration
0004 cover the sequence automatically.

## Ranking

Ranking weights live in `mem.ranking_profiles` as data, never as constants in
Python. Changing a weight means a new profile version, not an edit in place:
`mem.retrieval_events` stores the profile that produced each ordering, and
mutating a profile silently rewrites the explanation for every event already
recorded against it.

Ranking must be totally ordered. Ties break on `rrf_score`, then `id`. Sorting on
score alone leaves ties to arbitrary database row order, which on a homogeneous
corpus effectively becomes the ranking.

## Tests

Run `sh tests/run-all.sh`. RLS coverage runs first on purpose: a green write-path
suite on a leaking database is worse than no result, because it reads as
reassurance.

Test fixtures use a per-run suffix so re-runs do not collide with the temporal
unique constraint. `memory_app` has no DELETE grant, so cleanup is an owner-side
admin operation.
