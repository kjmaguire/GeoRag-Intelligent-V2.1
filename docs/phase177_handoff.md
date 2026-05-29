## Doc-phase 177 handoff — §12 ML-training skeleton workflow audit (no graduation; document remaining work)

**Status:** Audit-only tick + no code changes + 112/112 substrate verifier preserved.

## Why not graduate

The 4 remaining skeleton Hatchet workflows in the AI pool all
implement §12 (continuous-learning / ML training) capabilities.
Graduating any of them tonight would create more carry-overs than it
closes because each needs **real data + a non-trivial new dependency**.
This handoff captures the gates for each so the future graduation
ticks have a clean starting point.

## The 4 §12 skeletons

### `train_target_model` (§12.3, doc-phase 101 skeleton)

**Purpose:** Train an XGBoost target-scoring model per workspace
against accumulated `targeting.target_outcomes` rows. The model
predicts drill-hole success probability from candidate-zone features.

**Gate to graduation:**

1. **xgboost dependency** — not currently in `pyproject.toml`. Adding
   it pulls in numpy/scipy compatibility constraints already managed
   by sentence-transformers + Pydantic AI; should be merge-safe but
   needs explicit approval.
2. **Outcome accumulation** — `targeting.target_outcomes` is currently
   empty (0 rows). Training on 0 rows is meaningless; need ≥25 drilled
   targets with outcomes before first training fires. The
   `continuous_learning_loop` cron is designed to gate on this
   threshold.
3. **Feature schema** — `targeting.target_score_factors` carries the
   training features. Schema is live; needs the actual feature
   extraction pipeline from candidate zones to land.

**Estimated graduation work:** ~3 ticks (deps + feature extraction +
training body + persistence). Cannot graduate before drilling
outcomes accumulate, which is **months out** for any real workspace.

### `train_source_trust` (§12.7 / §21.5, doc-phase 102 skeleton)

**Purpose:** Train a per-workspace XGBoost model on
`silver.source_trust_features` to predict source-document
trustworthiness. Used by the orchestrator to weight citations from
high-trust sources higher in answer assembly.

**Gate to graduation:**

1. **xgboost dependency** — same as above.
2. **Citation feedback accumulation** — currently `silver.source_trust_features`
   needs population by an annotation pipeline that doesn't exist yet
   (would extract features from human-flagged answer-quality reviews).
   Today every source has the default trust score.
3. **The §10.11 Support Cockpit "flag this citation as wrong/right"
   button** — exists today as a UI placeholder but doesn't yet feed
   `silver.source_trust_features`. That wiring is the prerequisite.

**Estimated graduation work:** ~4 ticks (deps + UI feedback wiring +
feature pipeline + training).

### `field_outcome_learning` (§9.11, doc-phase 94 skeleton)

**Purpose:** Fold each newly-recorded drilling outcome into the
target-model's learning state. Bridges between operator-entered
outcomes and the next `train_target_model` invocation. Lighter than
training — it normalizes the outcome into feature rows, doesn't fit
a model.

**Gate to graduation:**

1. **`targeting.target_outcomes` write path** — exists today via
   `/admin/targeting` outcome-entry form. Functional.
2. **Feature extraction** — same gate as `train_target_model`
   (transforming raw outcomes into `target_score_factors` rows).
3. **No xgboost dependency needed** — this workflow is the "ETL into
   ML feature space" step, not the training step.

**Estimated graduation work:** ~2 ticks (feature extraction + fold
logic). Could land **before** `train_target_model` since it only
prepares features.

### `continuous_learning_loop` (§12.10, doc-phase 102 skeleton)

**Purpose:** Daily cron orchestrator. Walks each workspace's outcome
+ citation deltas, triggers `train_target_model` /
`train_source_trust` when thresholds cross, runs `evaluate_workspace`
on all active workspaces, records the pass/fail trend.

**Gate to graduation:**

1. **The 3 above workflows graduated** — otherwise the orchestrator
   chains into `NotImplementedError`.
2. **Workspace-iteration query** — needs a "find all workspaces that
   need retraining" SQL helper. Straightforward; not a blocker.
3. **Per-workspace eval threading** — `evaluate_workspace` is already
   live; this just fans it out.

**Estimated graduation work:** ~1 tick once the 3 dependent
workflows land.

## §12 graduation order (recommended)

```
1. field_outcome_learning   (no deps; just ETL)
2. xgboost dep + train_target_model (after outcomes accumulate)
3. UI feedback wiring → silver.source_trust_features (Support Cockpit)
4. train_source_trust (after feedback features populate)
5. continuous_learning_loop (orchestrator chain)
```

Realistic timeline: **6-12 months** to fully graduate §12, gated on
real drilling outcomes accumulating in production. Until then, the
skeleton workflows are registered (so worker startup is happy) but
fire `NotImplementedError` on invocation — which is correct behavior
(don't pretend to train on no data).

## What this audit is NOT

This is not a "graduate everything tonight" tick. The 4 skeletons
remain skeletons. The substrate verifier check `wf:*` still passes
(registration is the contract, not body completeness).

## Cumulative session state — 45 ticks closed

- **Doc-phase ticks this run:** **45** (132 → 177)
- **Substrate verifier:** **112/112 PASS**
- **Live pytest cases:** 286
- **Sections closed:** §25.4 + §6 + §04i validators (3 of 3 closeable)
- **§12 ML training:** properly scoped for future graduation (~6-12 months)
- **Hatchet AI pool:** 12 workflows registered (8 graduated, 4 skeleton-by-design)
- **Phase A ingestion:** staging at ~45% (200GB → container-local, ETA ~15 min)

## What's next

Tonight's remaining tractable work:
- **Phase A walk** once staging completes (ETA ~15 min)
- **Tier 3 smoke tests** on the ingested data + key app functions
- **Tier 4 multi-expert evaluation + scoring**

Long-term §12 ML work waits on drilling outcome accumulation
(months) + xgboost dep approval (operator decision).

## Carry-overs

- 4 skeleton workflows remain `NotImplementedError` — by design, not
  by oversight. Each has a documented gate above.
- A `targeting.target_outcomes` smoke-test fixture (e.g., 25 fake
  drilling outcomes per workspace) would unblock local development
  of `train_target_model` if a data-engineer wanted to prototype the
  feature pipeline without waiting on production data. Not autonomous-
  safe (synthetic ML training data has implications for downstream
  inference correctness).
