# Pinned eval corpus

Frozen copy of `.memory/` used as the Suite 1 benchmark corpus.

**Snapshot id:** `12d2cbc619bf41dc`
**Files:** 24

## Why this is a copy and not the live tree

04-EVALUATION.md §5 requires "the corpus snapshot pinned so results stay
comparable". Running the eval against the live `.memory/` tree means every new
ADR silently changes every score. That is not hypothetical here: adding one
document moved recall@5 by 0.05 and was very nearly reported as a regression in
the thing being measured.

A benchmark that moves when the product moves cannot answer "did this change
help", which is the only question the suite exists to answer.

## Updating it

Deliberate act, not a side effect:

    python eval/snapshot.py        # re-freeze from .memory/
    # then review and re-label any golden cases whose content hash changed

The command records the new snapshot id in `eval/golden_set.json`. Re-freezing
invalidates comparisons with earlier runs, so record the new snapshot id
alongside any results you keep.
