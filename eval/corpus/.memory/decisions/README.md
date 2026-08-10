# Decision Records

Drop these into `.memory/decisions/` at the root of the memory-platform repository. They are the first Plane A content the system will ingest, and the acceptance test for Phase 4 is that the platform can answer *"why did we reject a separate graph database?"* from them.

| ADR | Decision | Status |
|---|---|---|
| 0001 | PostgreSQL + pgvector as the single store | accepted |
| 0002 | Two-plane architecture (git knowledge / DB memory) | accepted |
| 0003 | Four-tool MCP surface with resources | accepted |
| 0004 | Stateless MCP protocol; explicit application state | accepted |
| 0005 | Trust lattice and quarantine | accepted |
| 0006 | Bi-temporal memory model | accepted |
| 0007 | RLS with FORCE, SET LOCAL, leak tests as a merge gate | accepted |
| 0008 | Hybrid retrieval with RRF | accepted |
| 0009 | Deterministic importance prior, learned utility | accepted |
| 0010 | PostgreSQL-backed job queue | accepted |
| 0011 | Working memory, goals and tasks out of scope | accepted |
| 0012 | Cross-project generalisation deferred and opt-in | accepted |
| 0013 | Provider abstraction | accepted |
| 0014 | Evaluation harness, permanent baseline, capability-scoped advantage | accepted |
| 0015 | Curation capacity — owner as initial curator | accepted |

---

## What changed from the draft set

**Requested tightenings, applied:**

- **0001** — added the pgvector operational requirement. `hnsw.iterative_scan = 'relaxed_order'` under scope predicates, `tenant_id`-leading composite indexes, recall measured *with* the filter applied, pinned minor version. Framed as correctness, not tuning.
- **0002** — added the human dependency as a first-class consequence, with the degradation path if no curator exists, and a link to 0015.
- **0007** — named the mechanisms explicitly: `FORCE` not `ENABLE`, `SET LOCAL` not bare `SET`, app role `NOBYPASSRLS` and not the table owner, clients unable to set `app.*` themselves.
- **0008** — the trigram/identifier arm is now called out in the decision list with its rationale, not buried in "trigram similarity."
- **All** — `related:` frontmatter added, so the decision graph is queryable once entity extraction lands.

**Reviewer disagreements, reconciled:**

- **0004 (correction #2).** Two reviews conflicted: one accepted "stateless" as written, one wanted it narrowed. The ADR locks *no protocol-session state* and explicitly declines to lock *the system is stateless*, permitting durable application state that is explicit (server-minted handle), durable (Postgres), and attributable. This is compatible with both reviews and prevents a redesign the first time a resumable operation appears.
- **0014 (correction #8).** The sharper conflict. One review wanted the hard "beat grep or don't ship" gate; the other wanted capability-scoped comparison. The ADR takes both: capability-scoped claims *and* the retained Phase-6 kill criterion at 3-of-5 headline metrics. Capability scoping is the right measurement frame; without a stop condition it becomes the mechanism by which projects run forever on the promise of the next feature. Softening the gate later is permitted — as its own ADR, with the reasoning recorded.
- **0006 (correction #5).** Both modifications folded in: the model is fixed as bi-temporal from day one, the PG18-unavailable emulation path is written down, and v1 temporal query ambition is explicitly constrained to four invariants.
- **0011 (correction #11).** The in/out boundary is now enumerated rather than described, so future scope arguments resolve against a list.

**New:**

- **0015** — the curator decision. Owner-as-curator, 30-minute weekly cap treated as a falsifiable architectural claim, automatic kill switch on auto-extraction at inbox depth 200 for two weeks, and a written degradation mode if the curator disappears.

---

## Open items these ADRs create for the other documents

| Document | Change needed | Why |
|---|---|---|
| `01-SCHEMA.sql` | Add the PG18-unavailable emulation variant for the temporal constraint (`valid_from`/`valid_until` + exclusion constraint) behind a migration branch | ADR-0006 |
| `05-BUILD-PLAN.md` | Move the git ingestion connector fully into Phase 1's vertical slice; move a minimal Review Inbox to Phase 2 so quarantine is never unattended | ADR-0002, ADR-0015 |
| `04-EVALUATION.md` | Restructure the Phase 6 gate around per-capability measurement, keeping the 3-of-5 headline gate as written | ADR-0014 |
| `02-MCP-CONTRACT.md` | No change — `pack_id` and task handles already implement ADR-0004's handle model | — |
| `00-MASTER-BLUEPRINT.md` | Update §0.2 wording for #2, #5, #8 to match the ADRs; add ADR-0015 to Appendix A | consistency |

One sequencing note worth deciding explicitly: the draft build plan puts the Review Inbox in Phase 5, but ADR-0015's kill switch assumes a place to review from the moment quarantine exists. Either the inbox moves earlier, or extraction stays deterministic-only until it lands. The second is cheaper and is probably the right call for a single-curator start.
