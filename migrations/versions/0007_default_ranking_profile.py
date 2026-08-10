"""Seed the default ranking profile.

mem.ranking_profiles exists but was empty, so there was nothing for the feature
reranker to read. 00-MASTER-BLUEPRINT.md §575 specifies the model:

    final = a*rrf_norm + b*trust + c*importance + d*utility
          + e*recency + f*entity_overlap - g*redundancy

and states that "all weights live in a versioned ranking_profiles table.
Changing a weight is a deployment gated by the eval suite". Weights therefore
live as DATA, not as constants in ranking.py — a code constant cannot be
A/B-compared, rolled back without a deploy, or pointed at by a stored
retrieval_event as the profile that produced a given ordering.

These starting values are a considered guess, not a measured result, and the
blueprint is explicit that they should not be tuned by intuition later: the
golden set (Phase 3) and 04-EVALUATION.md are what license a change. The row is
versioned `default@1` so the first tuned profile becomes `default@2` and old
retrieval_events keep pointing at the weights that actually produced them.

Rationale for the starting split:
  * rrf dominates (0.40) because it is the only term derived from the query.
    Everything else is a property of the memory and would, if over-weighted,
    return the same "important" rows regardless of what was asked.
  * trust is second (0.20): between two similarly relevant memories, the
    reviewed one wins. Not higher, or authoritative-but-irrelevant outranks
    exactly-what-you-asked-for.
  * utility starts at 0.10 but is gated to memories with >= 5 retrievals
    (ADR-0009), so a new memory is not buried by never having been used —
    the cold-start feedback loop that quietly makes new knowledge invisible.

Revision ID: 0007
Revises: 0006
"""
import json

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

WEIGHTS = {
    # Fusion and feature weights (the greek letters in the blueprint formula).
    "rrf": 0.40,
    "trust": 0.20,
    "importance": 0.10,
    "utility": 0.10,
    "recency": 0.10,
    "entity_overlap": 0.10,
    # MMR redundancy: lambda 0.7 per the blueprint, applied on digest embeddings.
    "mmr_lambda": 0.7,
    # Near-duplicate collapse threshold on digests.
    "dedup_cosine": 0.94,
    # Utility is ignored below this many retrievals (ADR-0009 cold-start guard).
    "utility_min_retrievals": 5,
    # Trust tier -> weight. authoritative 1.0 ... inferred 0.35 per §575.
    "trust_weights": {
        "authoritative": 1.0,
        "verified": 0.80,
        "observed": 0.60,
        "inferred": 0.35,
        "untrusted": 0.10,
    },
    # Recency half-life in days, per memory type. A decision stays relevant for
    # years; an episode is stale in a fortnight. One global half-life would make
    # the system either forget decisions or keep quoting last month's incident.
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
            "VALUES (%s, %s, true, %s) ON CONFLICT (id) DO NOTHING",
            ("default@1", json.dumps(WEIGHTS), json.dumps({"status": "unmeasured"})),
        )


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("DELETE FROM mem.ranking_profiles WHERE id = 'default@1'")
