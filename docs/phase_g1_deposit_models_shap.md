# Phase G.1 — Deposit-model templates + SHAP-equivalent scoring

**Status:** Complete. Master-plan §8 + §20.2 deliverable "ten deposit
model templates loaded with attributes" + "per-target SHAP-equivalent
breakdown via score factor table" both ship.

## What landed

### Seed (10 deposit-model templates)

`database/raw/phase8/10-deposit-model-templates-seed.sql` populates
`targeting.target_models` + `target_model_versions` with the master
plan's canonical 10:

| # | slug | commodity_primary | Use case |
|---|---|---|---|
| 1 | `athabasca_uranium` | uranium | Saskatchewan launch (McArthur River, Cigar Lake, Triple R) |
| 2 | `roll_front_uranium` | uranium | Wyoming / Texas / Nebraska ISR (Cameco Shirley Basin demo) |
| 3 | `orogenic_gold` | gold | Greenstone-belt lode (Detour, Macassa) |
| 4 | `epithermal_gold` | gold | Low/high-sulfidation (Yanacocha, Hishikari, Round Mountain) |
| 5 | `porphyry_copper` | copper | Cu-Mo, Cu-Au (Bingham, Chuquicamata, Oyu Tolgoi) |
| 6 | `vms` | copper | Cu-Zn-Pb-Au-Ag (Kidd Creek, Rio Tinto, Neves-Corvo) |
| 7 | `sedex` | zinc | Pb-Zn-Ag (Red Dog, Mount Isa, Sullivan) |
| 8 | `lithium_pegmatite` | lithium | LCT pegmatites (Whabouchi, Greenbushes, Tanco) |
| 9 | `oil_gas_basin` | petroleum | Permian, Williston, WCSB |
| 10 | `custom` | (empty) | Workspace-defined variants — clone + populate per-project |

Each row carries `attributes_payload` (host_rocks, structures,
alteration, geochemistry pathfinders + ratios + anomaly thresholds,
geophysical signatures, tectonic_setting), `positive_indicators` (each
with a weight), `negative_indicators` (each with a weight),
`analogues_payload` (3 named deposits per model with grade signal),
and `recommended_next_data` (the next-best-data menu for §20.5).

Each model gets a v1 row in `target_model_versions` with
`scoring_kind='weighted'`, `is_active=true`, and a flat
`factor_weights` JSONB combining positive (+) and negative (−)
indicators. Total: **10 models × 1 version = 10 active versions**.

The seed is `ON CONFLICT (slug) DO NOTHING` so re-running is safe; the
v1 INSERT for versions joins on `target_model_id` so it picks up newly
inserted models on subsequent runs.

### Scoring service

`src/fastapi/app/services/targeting/score_factors.py`:

* **`score_candidate_zone(zone_evidence, factor_weights, …)`** —
  weighted-additive scorer. For each factor in the model's
  `factor_weights`, computes `contribution = value × weight`,
  aggregates by `Σ contribution / Σ |weight|`, clamps to `[-1, 1]`.
* **`ScoreFactor`** dataclass carries the full SHAP-equivalent row
  shape (name, value, weight, contribution, evidence_chunk_ids,
  rationale) ready for INSERT into `targeting.target_score_factors`.
* **`persist_scored_zone()`** writes one `target_scores` row plus one
  `target_score_factors` row per factor, in a transaction. Idempotent
  on the `(zone_id, model_version_id)` unique key.

**Why this is SHAP-equivalent**: with a purely additive weighted scorer,
each `contribution_i = value_i × weight_i` *is* its Shapley value — no
nonlinearity for the attribution algorithm to untangle. The aggregate
score is the normalised sum. Phase 12's XGBoost path will swap in
`shap.TreeExplainer` once `target_outcomes` accumulate enough rows for
training; until then this is the explanation surface the master plan's
§8 "per-target SHAP-equivalent breakdown" line requires.

### Tests

`src/fastapi/tests/test_targeting_score_factors.py` — **10 tests, all
pass**:

* 8 pure-function tests (empty evidence, all-signals, negative
  weights, value clamping, missing factor, custom-empty path,
  rationale strings)
* 2 DB smoke tests: 10 models + 10 active versions present in the
  live database; a Cameco-shaped roll_front_uranium zone scores
  > 0.4 with the penalty factor contributing negatively.

Canary suite **post-G.1: 211 / 0** (+10 new tests, no regressions).

## What this unblocks

* **Phase 8 "Done when"** — the deposit-model template + SHAP-per-target
  prerequisites are now satisfied for any deposit type. The
  Athabasca-uranium + roll-front demos can both score real candidate
  zones end-to-end.
* **Phase 9 next-best-data UI** — the `recommended_next_data` arrays
  on every model are the data source for the §20.5 "Next-Best Data"
  recommendations on the Workspace Health Dashboard.
* **Phase 12 retraining** — `target_score_factors` is now populated
  with the per-factor breakdown the XGBoost path will compare its
  Shapley values against in A/B tests.

## Carry-overs

* **Signal collectors** that produce the `zone_evidence` dict (one
  per factor) are not yet wired. Today the dict is built by the
  caller; future phases will introduce per-factor collectors (an EM
  conductor detector, an alteration-pixel-coverage estimator,
  a U-gamma-log peak detector, etc.) that operate against silver
  data + Qdrant retrieval and produce the [0, 1] strengths.
* **Workspace variants** of `custom`: the per-workspace clone path
  (`target_models.create_workspace_variant()`) is documented in the
  seed but not yet implemented as a SQL function. Workspaces today
  must clone manually via the admin UI.
* **Uncertainty quantification** (`target_uncertainties` table) is
  not yet populated by the scorer. Adding a bootstrap variance over
  the per-factor observed strengths is a clean Phase 8 step 2 follow-up.

## Files added / changed

* **`database/raw/phase8/10-deposit-model-templates-seed.sql`** (new — applied)
* **`src/fastapi/app/services/targeting/__init__.py`** (new)
* **`src/fastapi/app/services/targeting/score_factors.py`** (new, 240 LOC)
* **`src/fastapi/tests/test_targeting_score_factors.py`** (new, 10 tests)
