"""Stage-1 query planner tests.

The planner is regex classification, so its failures are silent: a misclassified
query still returns results, just biased toward the wrong memory types. Nothing
errors, and the only visible symptom is a slightly worse ranking. That makes it
exactly the kind of component that needs table-driven tests rather than trust.

    docker compose exec -T api python - < tests/test_planner.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app/src")
from memory_platform import planner  # noqa: E402

results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((ok, name))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


# (query, expected intent). Phrased the way an agent actually asks, including the
# awkward ones — a planner tested only on textbook phrasings is tested on the
# queries that were never going to be hard.
CASES = [
    # rationale
    ("why did we reject a separate vector database?", "rationale"),
    ("why are we using RRF instead of weighted scores?", "rationale"),
    ("should we add Redis for caching?", "rationale"),
    ("do we need a message broker?", "rationale"),
    ("what was the trade-off behind one datastore?", "rationale"),
    # procedural
    ("how do I run the stack locally?", "procedural"),
    ("how do we deploy the api service?", "procedural"),
    ("what's the process for adding a migration?", "procedural"),
    ("walk me through the release steps", "procedural"),
    # recurrence
    ("have we seen this OOM before?", "recurrence"),
    ("has this happened previously in CI?", "recurrence"),
    ("did we hit this error again?", "recurrence"),
    # definitional
    ("what is Plane A?", "definitional"),
    ("what does the trust lattice mean?", "definitional"),
    ("who owns the ingestion pipeline?", "definitional"),
    # impact
    ("what breaks if I change the vector column?", "impact"),
    ("is it safe to remove the identifier arm?", "impact"),
    ("what depends on mem.fn_set_scope?", "impact"),
    # temporal
    ("what changed in the ranking profile?", "temporal"),
    ("when did we switch to Ollama?", "temporal"),
    ("what did we believe three months ago?", "temporal"),
    ("what did the config look like back then?", "temporal"),
    ("what was the schema in 2025?", "temporal"),
    # state
    ("what am I working on?", "state"),
    # no signal at all -> the general case, not a wrong guess
    ("postgres pgvector halfvec index tuning", "task-context"),
    ("embedder latency", "task-context"),
]


def main() -> None:
    print("\n1. Intent classification")
    for query, want in CASES:
        got = planner.plan(query).intent
        check(f"{want:12} <- {query[:44]}", got == want, got if got != want else "")

    print("\n2. Precedence: a temporal expression must not hijack impact/recurrence")
    # "what breaks if I change X" stays an impact question even with a year in it;
    # otherwise adding a date to any query silently rewrites what it is asking.
    check("impact survives a year mention",
          planner.plan("what breaks if I change the vector column in 2026").intent == "impact")
    check("recurrence survives 'last month'",
          planner.plan("have we seen this fail last month?").intent == "recurrence")
    check("procedural yields to a temporal expression",
          planner.plan("how do we answer what we believed three months ago").intent == "temporal")

    print("\n3. Plan contents")
    p = planner.plan("why did we pick Postgres over Qdrant?")
    check("rationale biases toward decision/constraint",
          set(p.memory_types) == {"decision", "constraint"}, str(p.memory_types))
    check("plan records what it matched on", bool(p.matched_on), str(p.matched_on))
    check("plan serialises for the retrieval event",
          {"intent", "memory_types", "stage"} <= set(p.as_dict()))

    p = planner.plan("ERR_TIMEOUT in HttpClient at memory_platform/db.py")
    found = set(p.identifiers)
    check("identifiers extracted from a pasted error",
          {"ERR_TIMEOUT", "HttpClient"} <= found, str(sorted(found))[:60])

    p = planner.plan("what breaks if I change mem.fn_set_scope?")
    check("impact asks for a 2-hop graph walk", p.needs_graph == 2, str(p.needs_graph))

    print("\n4. Robustness — the planner must never be a second failure mode")
    for bad in ("", "   ", "?", "a", "why " * 500):
        try:
            r = planner.plan(bad)
            ok = isinstance(r.intent, str) and r.intent != ""
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"      raised: {type(exc).__name__}")
        check(f"survives {bad[:14]!r}", ok)
    check("empty query falls back to the general case",
          planner.plan("").intent == "task-context")

    failed = [n for ok, n in results if not ok]
    print(f"\n{'='*62}\n{len(results)-len(failed)}/{len(results)} passed")
    if failed:
        for n in failed:
            print(f"  FAILED: {n}")
        sys.exit(1)


if __name__ == "__main__":
    main()
