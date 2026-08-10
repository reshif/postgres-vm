---
id: PROC-0002
title: Run the test suites and the retrieval eval
status: active
date: 2026-08-10
---

# Run the test suites and the retrieval eval

## Test suites

With the stack up, run `sh tests/run-all.sh`. It pipes each suite into the api
container, so a suite you just edited runs without rebuilding the image. It exits
non-zero if any suite fails and works unchanged as a CI gate.

Suites run in a fixed order and `test_rls_coverage` is first deliberately: if
isolation is broken, every other green result is misleading.

- `test_rls_coverage` — every table carrying `tenant_id` has RLS, FORCE and
  policies; plus a behavioural cross-tenant read attempt
- `test_isolation` — scoped writes, cross-tenant reads, write forgery
- `test_write_path` — tier assignment, quarantine, idempotency
- `test_ingest` — Plane A ingestion, secret rejection, supersession
- `test_context` — budget allocation, pack shape, retrieval event logging

## Retrieval eval (Suite 1)

`docker compose exec -T api python - < eval/run_eval.py`

It ingests `.memory/` into a dedicated eval tenant, runs every golden case, and
reports recall@k, MRR, nDCG@10 and forbidden@k against the gates in
`04-EVALUATION.md`. It exits non-zero when a gate fails.

## Rules

Do not tune ranking weights to make the eval pass. With a small hand-authored
golden set, fitting the weights to it produces a green suite and no information.
Weight changes are licensed by the eval set, not by intuition, and a change means
a new profile version.

If the corpus is homogeneous — all one memory type, one tier, one day — the
feature reranker cannot discriminate and the eval is measuring the embedding and
lexical arms alone. Diversify the corpus before drawing conclusions about ranking.
