"""Query planning, stage 1 — deterministic, always runs, sub-millisecond.

00-MASTER-BLUEPRINT.md §5.1 specifies a two-stage cheap-first planner. This is
stage 1: regex intent classification, identifier extraction, and temporal hints.
Stage 2 (a small-model call, cached by query hash) is explicitly optional and the
system "must be fully functional if it fails" — so stage 1 is built first and
stage 2 will layer on top rather than replace it.

WHY THIS EXISTS, measured rather than assumed: the golden set showed
recall@10 = 0.97 but recall@5 = 0.88. The right memory is almost always
retrieved and merely ranked too low, and the two worst cases are pure vocabulary
mismatches — "do we need a message broker?" should reach the Postgres-queue ADR,
which argues its case entirely in terms of LISTEN/NOTIFY and SKIP LOCKED and
never uses the phrase. Knowing the question is a `rationale` question about
infrastructure lets a decision-type memory win a fight it was losing on wording.

INTENT BIASES, IT DOES NOT FILTER. The blueprint's arm table applies p_types
inside each CTE, which is a hard filter. Stage 1 is regex classification and will
misclassify; a hard filter turns every misclassification into a guaranteed miss,
converting a ranking problem (recoverable, the item is at position 6) into a
recall problem (unrecoverable, the item is absent). So the plan feeds a rerank
term instead: it can only reorder candidates, never remove them.

MEASURED EFFECT ON THE CURRENT GOLDEN SET: none, and the reason is worth keeping.
That corpus is 15 ADRs, every one of them type `decision`. intent_match is
therefore constant within any single query — 1.0 for all candidates when the
intent maps to `decision`, 0.0 for all when it does not — so it cannot reorder
anything. Confirmed by A/B at weights 0 and 0.08: byte-identical metrics. This
module is not yet justified by evidence on this corpus; it is expected to matter
once the corpus contains procedures, failures and episodes alongside decisions,
and that expectation is itself untested.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Ordered: the first pattern that matches wins, so more specific intents are
# listed before the general ones they would otherwise be swallowed by
# ("what breaks if I change X" must not be read as a bare "what is").
INTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("impact", re.compile(
        r"\b(what breaks|breaks if|impact of|what depends on|safe to (change|remove|drop)"
        r"|if (i|we) (change|remove|delete|drop|rename))\b", re.I)),
    ("recurrence", re.compile(
        r"\b(have we (seen|hit|had)|has this (happened|occurred)|seen (this|it) before"
        r"|happened before|again\b|recurr)", re.I)),
    ("temporal", re.compile(
        r"\b(what changed|when did (we|it|this)|as of\b|back in\b|used to\b"
        r"|previously|history of|since when)\b", re.I)),
    ("procedural", re.compile(
        r"\b(how do (i|we|you)|how to\b|what.s the process|steps to\b|runbook"
        r"|procedure for|walk me through|how is .* (deployed|released|run))\b", re.I)),
    ("rationale", re.compile(
        r"\b(why (did|are|is|do|does|would|not|no)|reason(s)? (for|behind|why)"
        r"|rationale|what motivated|justif|trade[- ]?off|instead of\b"
        r"|should (i|we) (add|use|switch|adopt)|do we need\b|can (i|we) (add|use|switch))\b",
        re.I)),
    ("definitional", re.compile(
        r"\b(what is|what are|what does .* (mean|do)|define\b|meaning of|glossary"
        r"|who owns\b|where does .* live)\b", re.I)),
    ("state", re.compile(
        r"\b(what am i working on|current task|where was i|status of my)\b", re.I)),
]

# Intent -> memory types that most often carry the answer (§5.1 "Primary types").
PRIMARY_TYPES: dict[str, tuple[str, ...]] = {
    "rationale":    ("decision", "constraint"),
    "procedural":   ("procedure", "success"),
    "recurrence":   ("failure", "episode"),
    "definitional": ("entity_fact", "convention"),
    "temporal":     ("episode", "decision"),
    "impact":       ("entity_fact", "decision", "constraint"),
    "state":        ("session_summary",),
    "task-context": (),          # no bias; the project profile does the weighting
}

NEEDS_GRAPH: dict[str, int] = {
    "impact": 2, "definitional": 1, "rationale": 1, "task-context": 1,
}

TEMPORAL_BIAS: dict[str, str | None] = {
    "recurrence": "past-unbounded",
    "temporal": "window",
    "definitional": "current-only",
    "impact": "current",
    "state": "recent",
}

# Temporal expressions. 00-MASTER-BLUEPRINT.md §5.1 lists "temporal expression
# parsing" as part of stage 1 and it was missing.
#
# It matters more than it looks, because of ordering: "how do we answer what the
# team believed three months ago" hits the `procedural` pattern on "how do we"
# and gets classified as a runbook question, when the thing that actually answers
# it is the bi-temporal decision. A phrase like "three months ago" is stronger
# evidence of intent than the interrogative the sentence happens to open with,
# so a temporal expression anywhere in the query overrides an intent inferred
# from sentence shape alone.
#
# Spelled-out numbers are handled alongside digits. People write "three months
# ago" far more often than "3 months ago" in a question, and a digits-only
# pattern silently misses the common phrasing — caught by tests/test_planner.py,
# not by reading the regex.
TEMPORAL_EXPR = re.compile(
    r"""(?ix)
      \b(?:\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|
           eleven|twelve|couple\s+of|few)\s+
        (?:day|week|month|quarter|year)s?\s+ago\b
    | \b(?:last|past|previous)\s+(?:week|month|quarter|year)\b
    | \b(?:back\s+then|at\s+the\s+time|at\s+that\s+point|as\s+of\b)
    | \b(?:used\s+to|previously|formerly|originally)\b
    | \b(?:believed|thought|assumed)\s+(?:then|at|three|two|a\s+few)
    | \b(?:in|since|before|after)\s+(?:january|february|march|april|may|june|july
        |august|september|october|november|december)\b
    | \b(?:19|20)\d{2}\b
    """
)

_IDENT = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*(?:[._][A-Za-z0-9_]+)+\b"   # dotted.or_snake paths
    r"|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b"              # CamelCase
    r"|\b[A-Z][A-Z0-9_]{2,}\b"                             # ERR_CONSTANT
)


@dataclass
class QueryPlan:
    query: str
    intent: str
    memory_types: tuple[str, ...] = ()
    temporal: str | None = None
    needs_graph: int = 0
    identifiers: list[str] = field(default_factory=list)
    matched_on: str | None = None

    def as_dict(self) -> dict:
        """Stored on the retrieval_event: a ranking cannot be explained later
        without knowing which intent produced it."""
        return {
            "intent": self.intent,
            "memory_types": list(self.memory_types),
            "temporal": self.temporal,
            "needs_graph": self.needs_graph,
            "identifiers": self.identifiers[:10],
            "matched_on": self.matched_on,
            "stage": 1,
        }


def plan(query: str) -> QueryPlan:
    """Classify a query. Never raises, never returns None — the caller always
    gets a usable plan, because a planner that can fail becomes a second thing
    that has to be fallen back from."""
    q = (query or "").strip()
    intent, matched = "task-context", None
    for name, pat in INTENT_PATTERNS:
        m = pat.search(q)
        if m:
            intent, matched = name, m.group(0)[:40]
            break

    # A temporal expression overrides an intent inferred from sentence shape.
    # "how do we answer what the team believed three months ago" opens like a
    # procedure question and is not one. Only `impact` and `recurrence` outrank
    # it: "what breaks if I change X" and "have we hit this before" stay what
    # they are even when a date is mentioned.
    tspan = TEMPORAL_EXPR.search(q)
    if tspan and intent not in ("impact", "recurrence", "temporal"):
        intent = "temporal"
        matched = f"temporal expression: {tspan.group(0).strip()[:28]}"

    return QueryPlan(
        query=q,
        intent=intent,
        memory_types=PRIMARY_TYPES.get(intent, ()),
        temporal=TEMPORAL_BIAS.get(intent),
        needs_graph=NEEDS_GRAPH.get(intent, 0),
        identifiers=_IDENT.findall(q)[:20],
        matched_on=matched,
    )
