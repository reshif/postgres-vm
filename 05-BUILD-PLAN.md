# Build Plan

Nine phases. Each has a **single acceptance test** that is either true or false — not a checklist you can argue your way through. Do not start phase N+1 until phase N's acceptance test passes on real data.

Durations assume 1–2 engineers working steadily. They are not commitments; the sequence is the important part.

---

## Phase 0 — Contract (3–5 days)

Write these before any code. They are short — 2–4 pages each, not epics.

| Artifact | Contains |
|---|---|
| `ARCHITECTURE.md` | The two planes, service boundaries, the twelve corrections you accept or reject |
| `MEMORY_MODEL.md` | Types, trust lattice, lifecycle, bi-temporal semantics |
| `MCP_API.md` | The four tools, frozen (see `02-MCP-CONTRACT.md`) |
| `SECURITY.md` | Threat model, scope lattice, injection rules |
| `EVALUATION.md` | The seven suites and the go/no-go gates |
| `.memory/decisions/ADR-0001..0015` | This blueprint's decisions, in your own words (ADR-0015 = curation capacity) |

**Acceptance:** someone not in the room can read the six documents and correctly answer *"what happens when an agent writes a memory it inferred from a README?"*

**One decision to make here, not later (ADR-0015):** the kill switch on quarantine depth assumes somewhere to review from the moment quarantine exists. Choose now:

- **(a) Deterministic-only until the Inbox lands (recommended for a single curator).** No LLM extraction before Phase 5. Nothing enters quarantine that a rule did not put there. Cheapest, and it means an unattended queue is structurally impossible.
- **(b) Move the Inbox to Phase 2.** Higher upfront cost, but LLM extraction can start earlier.

The plan below assumes (a). If you pick (b), move the Phase 5 Inbox work into Phase 2 and keep everything else.

Dogfood note: the ADRs above are the memory-platform project's own first Plane A content. By Phase 4 the system must be able to answer "why did we reject a separate graph database?" from them.

---

## Phase 1 — Foundation + Plane A vertical slice (2.5 weeks)

Everything needed for one real, git-authored memory to travel end-to-end. Git ingestion moves here from its original later position: Plane A is the authoritative plane (ADR-0002), so a system that cannot ingest it is not a vertical slice of this architecture — it is a vertical slice of a different one.

- Postgres 18 + pgvector 0.8.2 in Docker; `btree_gist`, `pg_trgm`; migrations via Alembic.
- Schema from `01-SCHEMA.sql`: organizations, principals, projects, memories (with temporal unique constraint), memory_versions + trigger, embeddings, audit_log, ingestion_events.
- **RLS with FORCE from the very first migration.** Retrofitting isolation is a rewrite, and the negative tests must exist before there is data to leak.
- `fn_set_scope` + a connection wrapper that makes it impossible to open a transaction without it. Make the unsafe path unavailable, not merely discouraged.
- Embedding provider abstraction; BGE-M3 local; synchronous embed on explicit write.
- Memory CRUD with provenance, trust tier assignment, content hashing, token counting.
- pgTAP isolation suite wired into CI.
- Procrastinate job tables (jobs exist, workers can be trivial).
- **Temporal branch decided and pinned (ADR-0006).** If the target Postgres is 18+, use `WITHOUT OVERLAPS`. If not, use the exclusion-constraint emulation in `01-SCHEMA.sql` and run the temporal tests against both branches in CI.
- **`.memory/` spec + git ingestion** (moved up from Phase 2): `project.yaml`, `decisions/`, `procedures/`, `conventions.md`, `glossary.md` with frontmatter schema; webhook + poll ingestion; commit sha as `source_version`; one ADR = one memory, never chunked; idempotent by content hash; deleting a file archives its memory.
- **Secret scanning on the ingest path** (hard reject, never silent redaction).

**Acceptance:** merging a PR that adds `.memory/decisions/ADR-0007.md` makes that decision retrievable within 60 seconds with provenance resolving to the exact commit; a second scope context (project B) cannot see it — verified through the API *and* through raw SQL as `memory_app`; re-running ingestion creates no duplicates; a file containing a fake AWS key is rejected with a clear message and an alert. The pgTAP suite is green in CI.

---

## Phase 2 — Binding, CLI, and deterministic capture (1.5 weeks)

Plane A now ingests. This phase makes it usable day to day, and establishes the *only* write path that does not require a reviewer.

- `memory init` CLI: detects git remote, scaffolds `.memory/`, registers the project, writes `.mcp.json`, appends the memory section to `AGENTS.md`.
- `memory` CLI verbs: `status`, `search`, `why <topic>`, `doctor`.
- Server-side project binding verification against the registry — ambiguous binding is an error with a fix instruction, never a fallback to a broader scope.
- **Deterministic capture only** (ADR-0015 fallback mode, and the default until Phase 5): CI outcomes, merged PRs, commit metadata, tool exit codes. Rule-based classifiers, no LLM. Capped at trust tier `observed`, never authoritative, 30-day decay.
- Explicit `memory.write` path with server-assigned tiers (the tool arrives in Phase 4; the engine behind it is built here).
- Quarantine table exists and is enforced in retrieval, but nothing writes to it yet. The kill switch is wired and tested before there is anything to kill.

**Acceptance:** a green CI run on a real repository produces a `verified` episode retrievable within 60 seconds; a deterministic failure capture from a non-zero exit code lands at tier `observed`; nothing in the system can produce a quarantined row, and the retrieval path proves quarantined rows are excluded even when seeded manually.

---

## Phase 3 — Retrieval and the context engine (3 weeks) — **the core**

- Hybrid retrieval: vector + tsvector + trigram arms, RRF fusion (k=60).
- Query planner: deterministic stage 1; LLM stage 2 behind a flag with full fallback.
- Feature reranking with versioned `ranking_profiles`; MMR dedup.
- Context assembly: budget allocator, digest-first packs, deterministic section ordering, fill-percentage awareness.
- `retrieval_events` logging with the full score decomposition.
- Golden set v1 (100 cases minimum) + Suite 1 in CI.
- **HNSW under filter must be verified here**: `hnsw.iterative_scan='relaxed_order'`, measured recall with the scope predicate applied, not just unfiltered recall.

**Acceptance:** on the golden set, `recall@5 ≥ 0.90` and `p95 < 300 ms`; the Retrieval Debugger output for any query explains every returned and dropped item.

---

## Phase 4 — MCP server (2 weeks)

- Gateway targeting spec `2026-07-28`: stateless, `server/discover`, `_meta` version/capability handling, `resultType`, `ttlMs`/`cacheScope`, deterministic tool ordering.
- The four tools + the resource set.
- Auth: credential file for stdio; OAuth 2.1 + PKCE with audience validation for HTTP.
- Server-side project binding verification (never trust a client-supplied project id).
- MRTR for confirmations; Tasks extension for ingestion and re-embedding.
- OTel trace propagation from `_meta`.
- Connect **one** client first (Claude Code), verify, then add Cursor, then Codex.

**Acceptance — the milestone that matters most:**

> A completely new agent session, in a different client from the one that wrote the knowledge, connects to the MCP server, is correctly bound to the right project, retrieves the right historical context, and completes a task using knowledge created by a different agent in a different session — with zero information from any unrelated project appearing in the pack, verified in `retrieval_events`.

This is your source document's own stated first milestone, and it is the correct one.

---

## Phase 5 — Curation loop: Inbox, then LLM extraction (2.5 weeks)

This is where LLM extraction is switched on for the first time, and the ordering inside the phase is not negotiable: **build the Inbox first, then the extractor.** Until the Inbox is live, deterministic capture from Phase 2 is the only write path. That is what makes an unattended quarantine queue structurally impossible rather than merely discouraged (ADR-0015).

- Console skeleton: auth, project switcher, Inbox, Memory Detail.
- Session-end summarisation worker.
- Conservative extractor with a mandatory "nothing worth remembering" output; everything lands quarantined at tier 1.
- Injection heuristics; flagged items float to the top of the inbox.
- Accept / edit / merge / reject with reasons; keyboard-first; undo.
- Extraction acceptance-rate metric wired to a dashboard, with the 30–85% band alerting.
- **Curator instrumentation (ADR-0015):** inbox depth per project, weekly review minutes, alert at depth 100, automatic disable of LLM extraction at depth 200 sustained for two weeks. The kill switch is tested by simulation before extraction is enabled, not after.

**Acceptance:** a week of real sessions produces candidates; the curator triages 30 items in under 3 minutes and stays inside the 30-minute weekly cap; acceptance rate lands in the 30–85% band; no tier-1 memory is retrievable by default (verified by Suite 2); the auto-disable switch fires correctly in a simulated backlog.

---

## Phase 6 — Prove it (2 weeks) — **the go/no-go**

- Retrieval Debugger in the console.
- Suites 2–5 complete.
- End-to-end agent benchmark (Suite 7) with all five arms, including the filesystem baseline.
- Run it. Read the numbers honestly.

**Acceptance:** both gates in `04-EVALUATION.md` §7 —

1. the **capability scorecard** (C1–C6), which decides which capabilities ship enabled; and
2. the **headline gate** — arm D beats arm B on ≥3 of 5 metrics — which decides whether the platform continues at all.

Passing the scorecard on some capabilities while failing the headline gate means the capability is real and the packaging is not. Ship that capability inside the git convention and retire the platform. Genuinely stop. That decision is the mark of a serious team.

---

## Phase 7 — Structure: entities, graph, conflicts, consolidation (3 weeks)

- Entity extraction with canonical names + aliases; entity resolution.
- Closed-ontology relationships; inferred edges to `proposed_relationships` and the inbox.
- Graph retrieval arm (2-hop, scope-filtered); measure its contribution — if it adds <3% of returned items over a month, cut it.
- Conflict detection (write-time + nightly) and the console resolution flow.
- Consolidation workers: dedup, episode compaction, decay/archive, utility recompute.
- **Procedure distillation opens a git PR**, never a database write.

**Acceptance:** a seeded contradiction is detected within one nightly run, surfaces in the pack as contested, and resolves through the console with a full audit trail. Four consistent successful deploy episodes produce a procedure PR that a human merges.

---

## Phase 8 — Depth: timeline, graph UI, code index, feedback (4 weeks)

- Bi-temporal timeline with the as-of cursor wired across views.
- Entity graph (Sigma.js) with the time slider.
- Utility learning from `feedback` + retrieval→outcome correlation.
- `code-index` service: tree-sitter AST chunks, call/import graph, Merkle-diff incremental re-index — **gated on beating grep** on the code-navigation arm.
- Additional connectors (issues, CI, docs) in trust-tier order.

**Acceptance:** dragging the timeline to a past date changes the graph and the explorer consistently; utility scores measurably improve `nDCG@10` on the golden set; the code index either beats grep or is shipped disabled.

---

## Phase 9 — Hardening and scale-out (ongoing)

- Read replica for the console; partition `retrieval_events` monthly.
- Quarterly rebuild-from-git drill.
- Quotas, admission control, growth alerting.
- Second and third projects onboarded (`code-graph`, then `automation-platform` once restricted-sensitivity handling is proven).
- **Cross-project generalisation built but left off**, behind human approval, generalised content only.

---

## Sequencing rationale — what moved from your original 40-step list

| Your order | New position | Why |
|---|---|---|
| Isolation tests at step 12 | Phase 1, first migration | You cannot retrofit isolation, and you must not accumulate data before the tests exist |
| Git / Plane A ingestion mid-plan | **Phase 1**, inside the vertical slice | Plane A is the authoritative plane (ADR-0002); a slice without it is a slice of a different architecture |
| Extraction and the Inbox as one step | Split: deterministic capture Phase 2, LLM extraction + Inbox Phase 5 | ADR-0015 — an unattended quarantine queue must be structurally impossible, not merely discouraged |
| Provenance/confidence at steps 14–15 | Phase 1, in the base schema | They are columns, not a feature; adding them later means backfilling nulls forever |
| Hybrid retrieval at step 13 | Phase 3, from the start | Vector-only is not a shippable baseline for developer memory; identifiers and error strings need the lexical arm |
| Automatic extraction at step 23 | Phase 5, but only after the Inbox exists | Extraction without curation is how you get 10,000 memories and 200 useful ones |
| Dashboard at step 30 | Phase 5 (Inbox) and 6 (Debugger) | The Inbox is load-bearing infrastructure, not a reporting layer |
| Evaluation framework at step 32 | Phase 3 (golden set) and 6 (full) | You cannot tune retrieval without it; you cannot justify the project without it |
| Agent benchmarks at step 33 | Phase 6, as a **gate** | It decides whether to continue. That cannot be step 33 of 40 |
| Reflection at step 29 | Phase 7+, as a worker, output = inbox observations | Expensive, unreliable, and never authoritative |
| Repository/code ingestion at steps 37–38 | Phase 8, gated on beating grep | High effort, contested value |
| Cross-project generalisation at step 39 | Phase 9, built and left off | Most leak risk, least measured value |

---

## Team shape

| Role | Focus | Notes |
|---|---|---|
| Backend / retrieval | Context engine, retrieval, schema | The critical path; the most senior person belongs here |
| Backend / platform | MCP gateway, auth, ingestion, workers | Security perimeter; small, auditable code |
| Frontend | Console | Starts at Phase 5; can be part-time before that |
| Curator (rotating, ~30 min/week) | Inbox triage, conflict resolution, golden-set upkeep | **Named person.** This role is the difference between a system that improves and one that rots |

The curator role is not optional and is not "whoever has time." Assign it in Phase 0.

---

## What "done" looks like at each milestone (for stakeholders)

| Milestone | The demo |
|---|---|
| Phase 1 | "Project B literally cannot see project A's data — here's the SQL proving it" |
| Phase 2 | "I merged an ADR; 30 seconds later the agent can cite it, with a link to the commit" |
| Phase 3 | "Here is exactly why this memory ranked first, and what we dropped and why" |
| Phase 4 | "Cursor just used a decision Claude recorded yesterday, in a different session" |
| Phase 5 | "Five minutes of review a week keeps the whole thing clean" |
| Phase 6 | "Agents ask 40% fewer questions we've already answered — measured, against the honest baseline" |
| Phase 7 | "It caught that ADR-0009 contradicts the migration in issue #412, and told the agent to check" |
| Phase 8 | "Drag this to June and you can see what we believed then, and when we found out we were wrong" |
