"""ranking profile default@2 — adds the intent_match term.

A NEW PROFILE ROW, NOT AN EDIT TO default@1. mem.retrieval_events stores the
ranking_profile that produced each ordering, and the Retrieval Debugger's whole
job is answering "why was this ranked here" about a past query. Mutating
default@1 in place would silently rewrite the answer for every event already
recorded against it — every historical explanation would be reconstructed with
weights that were not in effect at the time. Versioning is what makes those
stored events mean anything.

default@1 is deactivated rather than deleted, for the same reason.

The new term: stage-1 query planning (planner.py) classifies intent and names
the memory types that usually carry that kind of answer. intent_match rewards a
candidate whose type matches. It is 0.08 — smaller than trust (0.20) and far
smaller than rrf (0.40) — deliberately:

  * The classifier is regex-based and will be wrong sometimes. A large weight
    would let a misclassification drag the wrong type to the top, converting a
    ranking near-miss into a confident wrong answer.
  * The measured problem is narrow. recall@10 was already 0.97; the loss was
    entirely in positions 1-5. That calls for a nudge that reorders near-ties,
    not a term that dominates.

This value is a considered guess like the rest of default@1, and 04-EVALUATION.md
governs changing it: the golden set is the license, not intuition. The suite is
the reason we can tell whether this helped at all.

Revision ID: 0009
Revises: 0008
"""
import json

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Carried forward from 0007 unchanged, plus intent_match. Restated in full rather
# than patched in SQL so the row is readable on its own — a profile you have to
# reconstruct from a chain of migrations is not auditable.
WEIGHTS = {
    "rrf": 0.40,
    "trust": 0.20,
    "importance": 0.10,
    "utility": 0.10,
    "recency": 0.10,
    "entity_overlap": 0.10,
    "intent_match": 0.08,
    "mmr_lambda": 0.7,
    "dedup_cosine": 0.94,
    "utility_min_retrievals": 5,
    "trust_weights": {
        "authoritative": 1.0,
        "verified": 0.80,
        "observed": 0.60,
        "inferred": 0.35,
        "untrusted": 0.10,
    },
    "recency_half_life_days": {
        "decision": 720,
        "constraint": 720,
        "convention": 540,
        "procedure": 360,
        "entity_fact": 360,
        "preference": 180,
        "failure": 120,
        "success": 120,
        "observation": 45,
        "episode": 21,
        "session_summary": 14,
    },
}


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(
            "INSERT INTO mem.ranking_profiles (id, weights, active, eval_score) "
            "VALUES (%s, %s, true, %s) ON CONFLICT (id) DO UPDATE "
            "  SET weights = EXCLUDED.weights, active = true",
            ("default@2", json.dumps(WEIGHTS),
             json.dumps({"status": "measured_by_suite_1", "supersedes": "default@1"})),
        )
        cur.execute("UPDATE mem.ranking_profiles SET active = false WHERE id = 'default@1'")


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("UPDATE mem.ranking_profiles SET active = true WHERE id = 'default@1'")
        cur.execute("UPDATE mem.ranking_profiles SET active = false WHERE id = 'default@2'")
