# Suite 1 results

Every row is measured against a named corpus snapshot. **Results from different
snapshots are not comparable** — that is the whole reason the corpus is pinned
(see `SNAPSHOT.md`).

Gates (04-EVALUATION.md §3): `recall@5 ≥ 0.90`, `MRR ≥ 0.75`, `nDCG@10 ≥ 0.70`,
`forbidden@10 = 0`.

---

## Snapshot `12d2cbc619bf41dc` — current reproducible baseline

33 memories (23 Plane A files + 10 Plane B seeds), 55 golden cases. Ranking
profile `default@2`; BGE-M3 via Ollama on CPU; cross-encoder reranking disabled.
The corpus fingerprint is `c3ab0ec62f77`.

| metric | result | gate |
|---|---:|---:|
| recall@1 | 0.533 | — |
| recall@5 | 0.800 | 0.90 ✗ |
| recall@10 | 0.900 | — |
| MRR | 0.694 | 0.75 ✗ |
| nDCG@10 | 0.728 | 0.70 ✓ |
| forbidden@10 | 0 | 0 ✓ |
| p95 latency | 443 ms | < 60,000 ms ✓ |

The previous snapshot included machine-local `binding.json` in its identity,
which made its drift warning non-actionable. The snapshot and evaluator now
exclude that local CLI state. This is a new baseline, so it must not be compared
to the measurements below. Phase 3 remains open because recall@5 and MRR fail.

### Failure classification

The five weakest cases were inspected with a 40-result diagnostic window. Their
expected memories were present at ranks 11–22, not absent from the candidate
set. The immediate problem is therefore semantic ranking, not an ANN/filter
recall failure: expand the traced benchmark before choosing a reranking or query
expansion change. The evaluator now prints those expected positions for every
weak case, and the debugger exports a review-only template with stable keys and
content hashes to make that expansion auditable.

---

## Snapshot `12234ea8ad9dff92` — corpus fingerprint `977030b6db0e`

33 memories (23 Plane A files + 10 Plane B seeds), 47 golden cases.
Ranking profile `default@2`. Embeddings bge-m3 via Ollama on CPU.

| metric | rerank OFF | rerank ON | gate |
|---|---|---|---|
| recall@1 | 0.518 | 0.691 | — |
| recall@5 | 0.787 | **0.876** | 0.90 ✗ |
| recall@10 | 0.840 | 0.926 | — |
| MRR | 0.682 ✗ | **0.829** ✓ | 0.75 |
| nDCG@10 | 0.704 ✓ | **0.832** ✓ | 0.70 |
| forbidden@10 | 0 ✓ | 0 ✓ | 0 |
| p95 latency | 816 ms | **5888 ms** | — |

### Reading this

**The cross-encoder is the single largest lever measured so far**: +0.089
recall@5, +0.147 MRR, +0.128 nDCG@10. It takes MRR from failing to passing on
its own.

**It is still off by default.** ADR-0013 requires it to earn its latency, and
5.9 s p95 is not interactively usable. That is a CPU property, not a design
property — the same model on a GPU is tens of milliseconds, at which point the
trade reverses completely. The flag exists so that decision can be made per
deployment instead of guessed once.

**recall@5 remains 0.024 short of the gate even with reranking.** Phase 3 is not
signed off. The remaining failures are vocabulary mismatches where no ranking
signal helps — "do we need a message broker?" must reach an ADR that argues for a
Postgres queue entirely in terms of `LISTEN/NOTIFY` and `SKIP LOCKED`.

### Known limitations of this measurement

- **47 hand-authored cases, not 150–300 traced ones.** 04-EVALUATION.md §3 wants
  the golden set built from real production queries. There is no production
  traffic yet, and cases written by the same person who wrote the ranker share
  its blind spots.
- **33 memories is a small corpus.** Every retrieval arm's internal limit
  (60/60/30/40/20) exceeds it, so each arm returns nearly everything and fusion
  is doing less work than it would at realistic scale.
- **The identifier arm contributed 0%** of expected hits across all cases. That
  is most likely correct rather than broken: it matches pasted symbols and error
  codes via trigram similarity, and no golden case pastes one. It means the arm
  is currently unmeasured, not that it is dead.

---

## Same snapshot, golden set extended to 55 cases

Added 8 identifier-shaped cases (`g48`–`g55`): queries where a developer pastes a
symbol or error string rather than describing the problem —
`permission denied for sequence memory_versions_id_seq`,
`exit code 137 OOMKilled false`, `hnsw.iterative_scan relaxed_order`.

| metric | rerank OFF | rerank ON | gate |
|---|---|---|---|
| recall@1 | 0.533 | 0.655 | — |
| recall@5 | 0.782 | **0.894** | 0.90 ✗ |
| recall@10 | 0.827 | 0.936 | — |
| MRR | 0.682 ✗ | 0.817 ✓ | 0.75 |
| nDCG@10 | 0.704 ✓ | 0.830 ✓ | 0.70 |
| p95 latency | 811 ms | 5988 ms | — |

### Why these cases were added

The identifier (pg_trgm) arm contributed **0%** of expected hits across the
original 47 cases, which looked like a dead arm. It was not — it was unmeasured.
That arm matches pasted symbols and error codes, and every case in the set was
written in prose, so it correctly matched nothing.

On the eight identifier-shaped cases, arm contribution to the expected memory:

| arm | share |
|---|---|
| semantic (vector) | 100% |
| lexical | 67% |
| identifier (trigram) | **22%** |

An arm at 0% is a candidate for deletion (§871 suggests removing arms
contributing <3% for a month). Deleting this one on that evidence would have
removed working functionality because the test set never asked it a question it
could answer. Coverage gaps look exactly like dead code.

`recall@5` is now **0.006 short** of the gate with reranking on.

---

## Same snapshot, 55 cases, with stage-1 temporal expression parsing

`00-MASTER-BLUEPRINT.md` §5.1 lists temporal expression parsing as part of stage
1; it was missing. "how do we answer what the team believed three months ago"
matched `procedural` on "how do we" and was routed as a runbook question.

| metric | rerank ON, before | rerank ON, after |
|---|---|---|
| recall@5 | 0.894 | 0.894 |
| recall@10 | 0.936 | **0.955** |
| MRR | 0.817 | 0.820 |
| nDCG@10 | 0.830 | 0.836 |

A small, real gain concentrated in recall@10. It does not move recall@5 and does
not close the gate.

---

## Where this stops, and why

`recall@5 = 0.894` against a gate of `0.90`, with reranking on. That is **0.006
— less than one case in 55.**

No further tuning was done, deliberately. Closing a gap that small against 55
hand-authored cases, written by the same person who wrote the ranker, would
produce a green suite and no information: the weights would be fitted to the
sample rather than to retrieval quality, and the eval would stop being able to
detect the next regression.

The four remaining failures were inspected individually and are genuine
retrieval failures, not labelling errors. Each is a vocabulary mismatch:

- *"do we need a message broker?"* → ADR-0010 argues for a Postgres queue
  entirely in terms of `LISTEN/NOTIFY` and `SKIP LOCKED`, never using the phrase.
- *"what stops project B reading project A's decisions?"* → ADR-0007 (RLS).
- *"how will we know the memory system actually helps?"* → ADR-0014 (evaluation).

What would honestly move these, in order of expected value:

1. **A larger, traced golden set** (04-EVALUATION.md wants 150–300 cases from
   real queries). 55 cases means one case is worth 1.8 points of recall@5 — the
   metric is too granular to tune against at this size.
2. **A bigger corpus.** At 33 memories every arm's internal limit exceeds the
   corpus, so fusion is barely doing its job.
3. **A faster cross-encoder** (GPU/hosted), which would make the +0.089 recall@5
   gain affordable enough to enable by default.

---

## Same snapshot, with the graph arm live (Phase 7a)

`mem.entities` and `mem.entity_mentions` were empty — nothing ever wrote to them
— so the graph arm returned nothing on every query. It measured 0% of expected
hits, which reads as a weak arm and was an **absent** one.

Deterministic entity extraction (dictionary + code patterns, no LLM per ADR-0015)
now runs on every write, with a backfill for memories written earlier.

| arm | before | after |
|---|---|---|
| semantic | 100% | 100% |
| lexical | 38% | 38% |
| identifier | 3% | 3% |
| **graph** | **0%** | **20%** |
| temporal | 70% | 70% |

11 of 55 golden queries resolve at least one entity, 34 entities and 96 mentions
across the corpus.

| metric | before | after (rerank OFF) |
|---|---|---|
| recall@1 | 0.533 | 0.515 |
| recall@5 | 0.782 | **0.818** |
| recall@10 | 0.827 | **0.882** |
| MRR | 0.682 | 0.686 |
| nDCG@10 | 0.704 | **0.719** |

The gain lands in the **default** configuration — the one that ships, with the
cross-encoder off. With reranking on the numbers are unchanged, because the
cross-encoder already reorders the same pool.

One methodology note: the first measurement showed no change at all, because
`eval/run_eval.py` called `memories.search()` without a scope, entity resolution
returned nothing, and the eval was measuring a four-arm system while the product
ran five. Fixed — but worth recording that a harness can silently omit the thing
it is measuring.

---

## Same snapshot, with relationships (Phase 7b) — CORRECTION

An earlier version of this section reported recall@5 **0.855** and attributed the
gain to relationship edges. **That attribution was wrong and the number is not
reproducible.**

`mem.relationships` for the eval corpus contains **zero rows**, so no relationship
could have influenced any of those measurements. The reason is a real gap rather
than a fluke: `link_relations` runs only on the CREATE path of `write_memory`,
and the eval corpus deduplicates on every run, so a corpus ingested before
relationship extraction existed never gets edges. `entities.backfill` fills
`entity_mentions` but has no relationship equivalent.

The reproducible figure on snapshot `12234ea8ad9dff92`, corpus fingerprint
`977030b6db0e`, verified across repeated runs and both alias configurations:

| metric | rerank OFF | gate |
|---|---|---|
| recall@5 | 0.818 | 0.90 ✗ |
| recall@10 | 0.882 | — |
| MRR | 0.686 | 0.75 ✗ |
| nDCG@10 | 0.719 | 0.70 ✓ |

So the honest standing is: **entity extraction is measured and helped**
(recall@5 0.782 -> 0.818); **relationship extraction is built and unit-tested but
its retrieval benefit is unmeasured**, because the benchmark corpus has no edges
to walk. Backfilling them is the prerequisite for any claim about the graph arm
at depth 2.

Recording this rather than quietly deleting it: a results file that only keeps
the numbers that looked good is worse than none, because the next person cannot
tell which claims were checked.

### Precision work that stands regardless

The first relation extractor asserted `PgBouncer -depends_on-> PostgreSQL` from
"PgBouncer is fronted by nothing; Procrastinate uses PostgreSQL" — it split only
on sentence-enders and then took the first entity on each side of the relation
phrase rather than the nearest. A false edge is worse than a missing one: the
graph arm then pulls unrelated memories together on every query touching either
entity, permanently. Fixed and covered by tests/test_entities.py.

### Follow-up: declared relations

Prose inference found 0 edges, so relations are now also **declarable** in
`.memory/` frontmatter:

    relates:
      - uses pgvector
      - contradicts Qdrant

Five ADRs were annotated, producing 5 edges. The corpus fingerprint is unchanged
(`977030b6db0e`) because frontmatter is stripped before content hashing, so this
is a clean A/B on the same documents.

| metric | no edges | 5 declared edges |
|---|---|---|
| recall@5 | 0.818 | 0.800 |
| recall@10 | 0.882 | 0.900 |
| MRR | 0.686 | 0.685 |
| nDCG@10 | 0.719 | 0.722 |

**Within noise.** Five edges over 33 documents is too sparse for the graph arm's
2-hop walk to reach much that the other arms miss. The mechanism is verified
(edges exist, the walk executes, tests cover it); the retrieval benefit is not
demonstrated and should not be claimed until a corpus with real edge density
exists.

The reason inference stays narrow is measured, not assumed: only 4 of 56 clauses
in a representative ADR contain two entities, and where they do the connector is
"+" or "with" rather than a relation verb. Matching prepositions would generate
an edge between every pair of technologies a sentence happens to name.
