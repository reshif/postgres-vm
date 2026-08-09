# The Knowledge Console — Frontend Specification

**One job:** make the memory system *inspectable and curatable* by a human in under ten minutes a week per project.

Not a dashboard. Not an admin panel. A **curation and debugging instrument** for a system that would otherwise be a black box that hallucinates silently.

---

## 1. Principles

1. **Trust is the visual primitive.** Every memory anywhere in the UI carries its trust tier as a persistent, non-negotiable visual encoding — a left border weight plus a tier glyph, identical in every view. A user should be able to tell authoritative from quarantined from across the room, without reading. This is the signature element of the interface, and everything else stays quiet around it.
2. **Read-heavy, write-narrow.** Almost every screen is read-only. The only write paths are triage decisions, curation edits, conflict resolution, pinning, and grants — all of which go through the same policy engine and audit trail as MCP writes. **The console never touches the database directly.**
3. **Throughput over beauty in the Inbox.** The reviewer is doing 30 decisions in 5 minutes. Keyboard-first, single-key actions, optimistic UI, undo. If triage takes more than ~3 seconds per item, curation does not scale and the whole system decays.
4. **Every number is a link to its evidence.** "3 conflicts" is a link. "84% hit rate" is a link to the underlying retrieval events. No dead statistics.
5. **Nothing is deleted from the UI.** Rejection archives with a reason. The audit trail is the product's spine.

### Design tokens

Dense operator tool, not a marketing surface. Deliberately not the warm-cream-and-serif look.

```
Surface        #0E1116 (base)   #161B22 (raised)   #1C2430 (overlay)
Ink            #E6EDF3 primary  #9AA7B4 secondary  #6B7785 tertiary
Trust ramp     authoritative #4CC38A · verified #57A6FF · observed #9AA7B4
               inferred #E3B341 · untrusted #F0616D          (also the only saturated colours used)
Accent         #7C7CF0  — interactive only, never decorative
Type           Display/UI: Söhne or Inter Tight, 14/20 base, tight tracking
               Data/code:  Berkeley Mono or JetBrains Mono — used for refs, sources, SQL, diffs
Radius         4px everywhere. Density: 32px row height in tables, 8px grid.
Motion         120ms ease-out on state change only. No ambient motion. Respect prefers-reduced-motion.
```

The trust ramp doubles as the only colour language in the product. That constraint is the point: if colour means trust everywhere, a coloured pixel is always information.

### Copy rules

Active voice, sentence case, name things by what the user controls. `Accept`, `Reject`, `Merge`, `Pin`, `Promote to project` — and the resulting toast uses the same verb (`Accepted`). Errors say what happened and what to do: *"Couldn't accept — this memory was superseded by ADR-0014 while you were reviewing. Reload to see the current version."* Empty states are invitations: *"No candidates waiting. Extraction runs at session end; new items land here."*

---

## 2. Information architecture

```
/                                Overview — all projects, health at a glance
/p/{project}
    /inbox        ★              Review queue: candidates, proposed edges, reflections
    /knowledge                   Explorer: filterable table of everything
    /knowledge/{ref}             Memory detail: content, provenance, versions, relations, usage
    /graph                       Entity graph, time-sliced
    /timeline                    Bi-temporal timeline
    /conflicts                   Contested points, resolution workflow
    /procedures                  Procedures with success history and failure modes
    /debug                       Retrieval Debugger  ★★
    /evals                       Eval suite results and experiment comparison
    /settings                    Profile, ingestion sources, ranking profile, grants
/audit                           Cross-project audit log
/admin                           Projects, principals, grants, models, jobs
```

★ = build first. ★★ = build second. Everything else follows.

---

## 3. Screen specifications

### 3.1 Review Inbox — the most important screen in the product

Your source documents list the graph view as a headline feature and never mention a review queue. Invert that. The graph is a demo; the inbox is what keeps the system alive.

```
┌─ payment-service · Inbox ─────────────────── 14 candidates · 3 edges · 2 observations ─┐
│  [All] [Candidates] [Edges] [Observations] [Conflicts]        ⌘K search   ? shortcuts  │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ ▌ FAILURE · inferred · session 2026-08-08 22:14 · claude-code                    1/14 │
│ ▌                                                                                      │
│ ▌ Ansible callback to ServiceNow times out at the default 30s under load; raising      │
│ ▌ the callback timeout to 60s resolved it.                                             │
│ ▌                                                                                      │
│ ▌ Evidence   session_01J8… (transcript ¶14–19) · commit a1b2c3d touches ansible.cfg    │
│ ▌ Entities   Ansible · ServiceNow · payment-service                                    │
│ ▌ Similar    mem_01J7… "callback timeout" (0.91)  → merge?                             │
│ ▌ ⚠ No verified evidence. Accepting records this as tier 2 (observed).                 │
│ ▌                                                                                      │
│ ▌ [a] accept   [e] edit & accept   [m] merge into similar   [r] reject   [s] skip      │
│ ▌ [p] promote to procedure (opens PR)        [→] next        [u] undo last             │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

Requirements:
- **Single-key actions**, `j/k` navigation, `u` undo with a 10-second window, batch select with `x`.
- **Similarity surfaced inline.** The most common correct action is *merge*, and if merging is harder than accepting you will accumulate near-duplicates.
- **Reject requires a reason** from a short list (`noise` / `wrong` / `already known` / `too specific` / `unsafe`). Reasons feed the extractor-quality metric and the training set for tightening prompts.
- **Flagged items float to the top**: anything the injection heuristic caught, anything touching `restricted` sensitivity, anything with no evidence.
- **Age indicator + expiry countdown.** Candidates expire in 14 days; show it.
- **Session grouping** so a reviewer can accept/reject a whole session's output when it is obviously good or obviously noise.

Target: 30 items triaged in under 3 minutes by a practised reviewer.

### 3.2 Knowledge Explorer

Virtualised table (TanStack Table + TanStack Virtual). Columns: trust ▌, type, title, scope, valid from → until, source, last used, uses, tokens, status. Faceted filters as URL state so views are shareable and bookmarkable. Saved views per project (`Contested`, `Stale >90d`, `Unverified`, `Never retrieved`, `Pinned`).

The columns that create behaviour change: **`uses` and `last used`.** A memory retrieved zero times in 90 days is a candidate for archival, and seeing that column sorted ascending is the fastest way to understand that more memory is not better.

Row expansion shows digest + provenance without navigation. Bulk actions: archive, pin, re-scope (MRTR-confirmed), re-embed.

### 3.3 Memory Detail

```
┌ ADR-0007 · Use PostgreSQL + pgvector as the memory backend ─────────────────┐
│ ▌ AUTHORITATIVE   project: memory-platform   valid 2026-07-25 → open        │
│                                                                             │
│ [Content] [Provenance] [History] [Relations] [Usage] [Raw]                   │
│                                                                             │
│ Provenance ── git://memory-platform@a1b2c3d · .memory/decisions/ADR-0007.md │
│               authored by ram · merged in PR #88 · ingested 2026-07-25      │
│               [view file] [view diff] [view PR]                             │
│                                                                             │
│ History ──── v3  2026-07-25  supersedes ADR-0003 (Redis)                    │
│              v2  2026-06-02  scope narrowed to project                      │
│              v1  2026-05-18  created                    [compare v1 ↔ v3]   │
│                                                                             │
│ Relations ── supersedes → ADR-0003 · supported_by → benchmark-2026-07-20    │
│              affects → Context Engine, Retrieval                            │
│                                                                             │
│ Usage ────── retrieved 47× in 90d · 12 sessions · 4 agents                  │
│              ▁▂▅█▆▃▂  last: 2h ago (claude-code, "why pgvector")            │
└─────────────────────────────────────────────────────────────────────────────┘
```

Provenance links resolve to the actual git file and PR. That single detail is what converts skeptics.

### 3.4 Entity Graph

**Library choice, decided on evidence rather than taste:** the practical 2026 split is Cytoscape.js for graph *analysis* plus visualisation, vis-network for interactive diagrams, and Sigma.js as a WebGL renderer over graphology for large graphs. Ship **Sigma.js + graphology**: your graph will pass 5k nodes in a mid-size org, layout and label rendering dominate the experience, and graphology gives you centrality and pathfinding separately from rendering. Benchmark with a realistic fixture — node-count claims do not transfer between graph shapes.

Behaviours that make it useful rather than decorative:
- **Never render the whole graph.** Default view = 2-hop neighbourhood of the entity you arrived from. A full-graph "overview" mode is a separate, sampled, clustered view.
- **Time slider on valid time.** Drag to June and the `uses → Nautobot` edge is live; drag to August and it is greyed with `NetBox` active. This is the payoff for bi-temporality, and it is the demo that makes people understand the system.
- Edge thickness = confidence; edge colour = trust ramp; dashed = proposed (unreviewed).
- Click an edge → provenance panel: which memory asserted this, when, from what source.
- **Impact mode:** select a node, highlight everything reachable via `depends_on`/`part_of` — "what breaks if we change PostgreSQL."
- Server-side layout precomputation for graphs over ~3k nodes; cache positions per project.

### 3.5 Timeline

Two lanes, because bi-temporality has two axes:

```
VALID TIME  (when things were true)
 ──●────────────●──────────────────●─────────────────────────────►
   PG15         PG17               pgvector 0.8.2
                                                    ┊ as-of cursor
RECORD TIME (when we learned)
 ─────●───────────────●──────●───────────●──────────────────────►
      ADR-0003        incident  ADR-0007  procedure distilled
```

Dragging the as-of cursor updates **every other view in the app** (graph, explorer, detail). Answering "what did we believe on 15 June, and when did we find out we were wrong?" in two drags is the single most compelling thing this UI can do.

Rendering: visx or Observable Plot over canvas; virtualise beyond ~2k events.

### 3.6 Retrieval Debugger — build this second

```
Query: "why did we choose pgvector?"        [profile: default@7 ▾] [as-of: now] [Run]

PLAN        intent=rationale  entities=[pgvector, PostgreSQL]  types=[decision,constraint]
            scope={org, project:memory-platform}  window=none        (12ms, cached)

ARMS        semantic  60 →  ADR-0007(1) ADR-0003(4) bench(7) …            (28ms)
            lexical   23 →  ADR-0007(1) bench(2) …                        (9ms)
            identifier 6 →  ADR-0007(1) …                                 (4ms)
            graph     14 →  bench(1) ADR-0003(3) …                        (17ms)
            temporal   0                                                  (2ms)

FUSION      ADR-0007  rrf .0489 │ trust +.20 imp +.09 util +.06 rec +.01 = .81   ✓ returned
            bench     rrf .0221 │ trust +.15 imp +.04 util +.00 rec +.02 = .43   ✓ returned
            ADR-0003  rrf .0198 │ trust +.20 imp +.09 util +.01 rec -.08 = .31   ✓ returned as
                                  superseded context
            mem_9f2   rrf .0102 │ …                                       ✗ dropped: MMR dupe of ADR-0007

PACK        3 items · 612 tokens of 4000 budget · p95 stage latency shown above
            [compare with profile default@6]  [export as eval case]  ★
```

The two starred affordances are what make this a tool rather than a readout:
- **Compare profiles side by side** — diff which memories entered/left and why.
- **Export as eval case** — one click turns a real production query into a golden-set entry with the correct answer marked. This is how the eval set stays representative instead of going stale; rebuild it from production traces every quarter.

Also: replay any historical `retrieval_events` row by `pack_id`, so "why did the agent say that yesterday?" is answerable exactly.

### 3.7 Conflicts

Side-by-side diff of the two assertions, each with trust, valid time, source, and author. Resolution actions: `A supersedes B`, `B supersedes A`, `Both valid (different scope/time)`, `Merge into one`, `Escalate`. Resolutions are recorded with a reason and an actor. Unresolved conflicts show where they have leaked into context packs, and how often — that count is the argument for resolving them.

### 3.8 Project Health

```
payment-service                              health 78/100  ▾ from 84 last week

Active 1,247   Quarantined 14   Archived 301   Contested 3 ⚠   Stale >90d 127 ⚠
Extraction acceptance 41%       Retrieval hit rate 84%        p95 context 287ms
Growth +38/wk (▲ from +12 — check the extractor)              Never retrieved 22%

Top retrieved                        Never retrieved (candidates for archival)
1. ADR-0007  47×                     · 214 episodes from May
2. deploy-procedure  31×             · 41 session summaries
3. callback-timeout failure  22×     · 18 entity facts
```

`health` is a transparent composite (contested, staleness, acceptance rate, hit rate, growth anomaly), and hovering shows the formula. An opaque health score is worse than none.

### 3.9 Evals

Runs over time as a line chart per suite; a run detail with per-case pass/fail and diffs; and an experiment comparison view (profile A vs B on the same golden set). CI posts results here and to the PR.

---

## 4. Technical shape

| Concern | Choice | Note |
|---|---|---|
| Framework | Next.js (App Router), React Server Components for read views | Tables and graphs are client components |
| Data | TanStack Query; server actions for mutations | Optimistic updates in the Inbox, with rollback |
| Tables | TanStack Table + Virtual | 100k rows must scroll at 60fps |
| Graph | Sigma.js + graphology | See §3.4; server-side layout above ~3k nodes |
| Charts | visx / Observable Plot | Canvas for timelines |
| Editor | CodeMirror 6 for memory/ADR editing | Markdown + frontmatter, diff view |
| Styling | Tailwind + shadcn/ui, restyled to the token set above | Do not ship shadcn defaults |
| Auth | Same OIDC provider as the MCP gateway | Console permissions ⊆ principal scopes |
| Realtime | SSE from `context-api` for job progress and inbox arrivals | No websockets needed |
| API | REST/RPC on `context-api` — never direct DB | Same policy engine, same audit |

**Accessibility floor (not optional):** full keyboard operation of the Inbox, visible focus rings, AA contrast on the trust ramp against both surfaces, `prefers-reduced-motion` honoured, graph views have a table equivalent (a WebGL canvas is not accessible and must never be the only path to information).

**Performance budgets:** first contentful paint < 1.2s on the Inbox, table filter response < 100ms client-side, graph interaction ≥ 30fps at 5k nodes.

---

## 5. Build order

| Order | Screen | Why here |
|---|---|---|
| 1 | Inbox | Without curation, quality decays from week one |
| 2 | Retrieval Debugger | Without it you tune ranking blind |
| 3 | Explorer + Memory Detail | The "what does it actually know?" question |
| 4 | Conflicts | Arrives with real usage |
| 5 | Project Health | Once there are enough numbers to be meaningful |
| 6 | Timeline | Needs bi-temporal data density to be interesting |
| 7 | Graph | Highest demo value, lowest operational value — resist doing this first |
| 8 | Evals + Audit | Formalise once the harness exists |
