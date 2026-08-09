# Universal Agent Memory Platform — Build Package

A consolidated, build-ready engineering blueprint derived from your two architecture documents, reconciled against the state of the field as of **9 August 2026**.

## Files

| File | What it is | Read when |
|---|---|---|
| `00-MASTER-BLUEPRINT.md` | The full argument: research grounding, twelve corrections to your source docs, architecture, memory model, security, retrieval, ingestion, dev workflow, ops, risks | First, end to end |
| `01-SCHEMA.sql` | Complete Phase 1–4 PostgreSQL schema: bi-temporal memories, RLS with FORCE, embeddings, graph, telemetry, hybrid search function, pgTAP gate | When you start Phase 1 |
| `02-MCP-CONTRACT.md` | The four-tool MCP surface, targeting spec revision `2026-07-28`, plus a conformance checklist | Before Phase 4; freeze it in Phase 0 |
| `03-FRONTEND-KNOWLEDGE-CONSOLE.md` | The knowledge frontend: principles, design tokens, screen-by-screen spec, library choices, build order | Before Phase 5 |
| `04-EVALUATION.md` | Seven suites, five benchmark arms, CI gates, golden-set governance, go/no-go criteria | Phase 0 (write the gates), Phase 3 (first suite), Phase 6 (the decision) |
| `05-BUILD-PLAN.md` | Nine phases, each with one binary acceptance test; team shape; what moved from your original 40-step order and why | Immediately after the blueprint |
| `docker-compose.yml` | Local-first stack: PG18 + pgvector 0.8.2, local embeddings, API, MCP edge, workers, console, OTel | Day one |
| `decisions/ADR-0001..0015` | The signed-off decision records. Copy into `.memory/decisions/` in the repo | Phase 0 output; the system's own first Plane A content |

**Revision, 9 Aug 2026 — decisions locked.** All twelve corrections accepted, three with modifications (#2 protocol-session state only, #5 model fixed with a PG<18 emulation path, #8 capability-scoped advantage with the stop condition retained), plus ADR-0015 on curation capacity. The schema, evaluation gates and build plan below reflect the locked set.

## The five things to take away

1. **Two planes.** Curated project knowledge lives in git under `.memory/` and is reviewed like code. The database is a derived index plus the episodic layer. This gives you trust, review, portability and disaster recovery in one structural move — and it gives you a fallback product if the platform never beats its baseline.

2. **Four MCP tools, not eleven, on a stateless server.** `context`, `search`, `write`, `explain`. The July 2026 spec removed sessions and the initialize handshake; tool-count bloat measurably degrades agent performance. Everything the other seven tools did is reachable through parameters, resources, or the console.

3. **Auto-extraction is a security boundary.** A memory system that ingests conversations and repos is a persistence layer for indirect prompt injection. Trust lattice, quarantine, human promotion, injection heuristics, secret rejection. Nothing an LLM inferred becomes authoritative without a human.

4. **The Review Inbox matters more than the knowledge graph.** Build the triage queue first. If curation does not take under ten minutes a week per project, quality decays and no amount of retrieval sophistication saves it.

5. **Beat the honest baseline or ship the simpler thing.** The baseline is a good `AGENTS.md` plus grep plus the filesystem — a genuinely strong competitor with published evidence behind it. Phase 6 is a real go/no-go, with real numbers, against that arm.

## The first milestone

> A completely new agent session, in a different client from the one that wrote the knowledge, connects to the MCP server, is correctly bound to the right project, retrieves the right historical context, and completes a task using knowledge created by a different agent in a different session — with zero information from any unrelated project appearing in the pack, verified in the retrieval log.

That is your source document's own stated milestone, and it is still the right one. Phases 1–4 exist to reach it; everything after is incremental evolution of the same foundation.
