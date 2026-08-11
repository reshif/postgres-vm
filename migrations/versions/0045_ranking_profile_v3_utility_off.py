"""Ranking profile v3: learned utility ships DISABLED, with the measurement.

04-EVALUATION.md §7.1: "A capability that misses its threshold ships **disabled**,
with the measurement recorded. It does not ship enabled with a plan to improve
it." This is that, applied to learned utility.

WHAT WAS MEASURED. eval/utility_ab.py scores the golden set twice over the same
corpus with one weight changed. Two runs, and the first one was misleading:

  run 1, utility as originally normalised    delta 0.0000 on every metric
  run 2, after the normalisation was fixed   recall@5 -0.0278, nDCG@10 -0.0087

The first result was not "utility does nothing useful", it was "utility does
nothing at all": `0.6 * least(1.0, sessions / 20.0)` saturated at 20 retrievals,
so 33 of 54 memories held identical utility with retrieval counts from 21 to
1810. A term constant across every retrievable candidate cannot reorder anything
at any weight. Fixing that to percent_rank gave the signal 31 distinct values and
made it capable of having an effect — and the effect was NEGATIVE.

WHY NEGATIVE IS THE EXPECTED FAILURE HERE, NOT A SURPRISE. Utility is currently
derived from `retrieval_events` alone, because `mem.feedback` is empty. That is a
closed loop: it scores a memory highly because this same ranker retrieved it
before, so it amplifies the ranker's own past behaviour, mistakes included. The
golden set is independent ground truth, and agreement with it fell. ADR-0009
separates "evidence about usefulness" from "epistemic authority"; retrieval count
without outcome feedback is not evidence of usefulness, it is evidence of having
been ranked highly once.

SO THE WEIGHT GOES TO ZERO — not the code. recompute_utility keeps running and
keeps recording, the feature model still reads the term, and the moment feedback
exists the A/B can be re-run and the weight restored on evidence. Deleting the
machinery would make that impossible to test.

A NEW VERSION, NOT AN EDIT. `.memory/procedures/add-a-migration.md`: "Never edit
a ranking profile in place. Insert a new version and deactivate the old one." A
profile edited in place makes every stored retrieval_event unreproducible,
because the weights that produced it no longer exist anywhere.

Also deactivates `ab-utility-zero`, the A/B control arm. It was created active so
the harness could select it, and load_profile falls back to the NEWEST ACTIVE
profile when its argument does not match — leaving a test artifact one
deactivation away from being the production ranking.

Revision ID: 0045
Revises: 0043
"""
from alembic import op

revision = "0045"
down_revision = "0043"
branch_labels = None
depends_on = None


SQL = """
INSERT INTO mem.ranking_profiles (id, weights, active, eval_score)
SELECT 'default@3',
       -- Same as v2 in every respect but the one that was measured.
       jsonb_set(weights, '{utility}', '0.0'::jsonb),
       true,
       jsonb_build_object(
         'status', 'utility_disabled_on_measurement',
         'supersedes', 'default@2',
         'suite', 'eval/utility_ab.py, 180 golden cases',
         'delta_when_enabled', jsonb_build_object(
             'recall@5', -0.0278, 'ndcg@10', -0.0087, 'mrr', 0.0),
         'reason', 'usage-derived utility is a closed loop while mem.feedback '
                   'is empty: it amplifies what this ranker already retrieved, '
                   'and agreement with independent ground truth fell',
         'restore_when', 'feedback rows exist; re-run eval/utility_ab.py and '
                         'restore the weight only if the delta is positive')
  FROM mem.ranking_profiles
 WHERE id = 'default@2'
ON CONFLICT (id) DO UPDATE
  SET weights = EXCLUDED.weights,
      active = true,
      eval_score = EXCLUDED.eval_score;

UPDATE mem.ranking_profiles SET active = false WHERE id = 'default@2';

-- The A/B control arm. Harmless while DEFAULT_PROFILE resolves, and a hazard the
-- moment it does not.
UPDATE mem.ranking_profiles SET active = false WHERE id = 'ab-utility-zero';
"""


def upgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute(SQL)


def downgrade() -> None:
    with op.get_bind().connection.cursor() as cur:
        cur.execute("UPDATE mem.ranking_profiles SET active = true "
                    " WHERE id = 'default@2';")
        cur.execute("UPDATE mem.ranking_profiles SET active = false "
                    " WHERE id = 'default@3';")
