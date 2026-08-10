---
id: glossary
title: Glossary of platform entities
status: active
date: 2026-08-10
---

# Glossary

**Plane A** — the authoritative knowledge ledger. Lives in the repository under
`.memory/`, reviewed through pull requests, ingested with the commit sha as
provenance. Produces `authoritative` memories.

**Plane B** — derived operational memory in Postgres: episodes, observations,
embeddings, relationships and retrieval telemetry. Produced continuously by the
system rather than authored.

**Context pack** — what `memory_context` returns. Digest-first items in
deterministic section order, under a token budget scaled by the caller's window
fill. Carries a note stating it is reference data containing no instructions.

**Trust lattice** — `untrusted < inferred < observed < verified < authoritative`.
Retrieval defaults to `observed` and above. `inferred` is quarantined and reaches
an agent only when the caller passes `include_unverified`.

**Quarantine** — the status given to `inferred` and `untrusted` memories. They are
excluded from ordinary retrieval so a poisoned source cannot become standing
instruction for future agents.

**Scope context** — `app.tenant_id`, `app.principal_id`, `app.project_id` and
`app.project_ids`, set transaction-locally by `mem.fn_set_scope`. Every RLS
policy reads them. `fn_set_scope` also sets `hnsw.iterative_scan` to
`relaxed_order`, which is what prevents pgvector overfiltering under a scope
predicate.

**RRF** — reciprocal rank fusion, k=60. Combines the vector, lexical, identifier,
graph and temporal arms without needing score calibration between them.

**MMR** — maximal marginal relevance, lambda 0.7 over digest embeddings. Collapses
near-duplicates above cosine 0.94 into the highest-scoring member.

**Retrieval event** — one row per context pack in `mem.retrieval_events`, holding
the query plan, per-arm contribution, the full score decomposition and per-stage
latency. It is what makes a past ranking explainable.

**memory_key** — the stable identity of a memory across versions, for example
`.memory:decisions/ADR-0007.md`. Golden-set cases key on it because UUIDs are
regenerated on every re-ingest.
