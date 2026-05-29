## Doc-phase 86 handoff — §8.4 Target Recommendation Graph state + node stubs

**Status:** Complete. 23-field state model + 12 §18.2 node stubs imported clean.

## What landed

New package `app/services/target_recommendation/`:
- `state.py` — Pydantic `TargetRecommendationState` model (23 fields)
  with sub-models `CandidateZone`, `ScoreFactor`, `ZoneScore`,
  `UncertaintyEntry`, `RankedTarget`. `ScoringKind` enum
  ("weighted"/"xgboost"/"ensemble").
- `nodes.py` — 12 async node stubs matching §18.2 verbatim:
  `select_commodity_deposit_model → load_workspace_playbook →
  collect_private_evidence → collect_public_geoscience →
  generate_candidate_zones → score_candidate_zones →
  calculate_uncertainty → apply_constraints → rank_targets →
  explain_score_factors → create_map_layers → route_to_review_cockpit`.
  Each raises NotImplementedError; signatures + docstrings lock the
  contract.
- `__init__.py` — re-exports state + all 12 nodes.

### Design notes

- **Per-zone fan-out** documented in module docstring. `score_candidate_zones`,
  `calculate_uncertainty`, `explain_score_factors` are per-zone but
  this skeleton operates on the whole state slice; the §8.6 Hatchet
  workflow handles parallelism via task fan-out (same pattern as
  `ingest_pdf.persist` step's per-block parallelism).
- **`apply_constraints` v1 scope** = exclusion polygons only (per §8
  scope proposal recommendation). v2 = regulatory + access.
- **`route_to_review_cockpit` pause-resume** — Hatchet workflow pauses
  here for R5 sign-off; resumes on Reverb event from admin UI.
- **`explain_score_factors`** notes the "never say drill here" rule
  from §18.1; the Recommendation Explainer Agent (§8.12) enforces.

### State model — 23 fields

Identity (5): run_id, workspace_id, project_id, requested_by_user_id,
aoi_geom_wkt.

Model + scoring (4): target_model_id, target_model_version_id,
scoring_kind, workspace_playbook.

Evidence (2): private_evidence, public_evidence.

Zones (2): candidate_zones, excluded_zone_ids.

Scoring (3): scores, uncertainties, ranked_targets.

Map outputs (1): map_layer_uris.

Routing (2): sent_to_review_cockpit, review_cockpit_url.

Telemetry (3): started_at, completed_at, failure_reason.

Schema versioning (1): schema_version.

### Smoke test

    docker exec georag-fastapi python -c "
        from app.services.target_recommendation import TargetRecommendationState
        print(len(TargetRecommendationState.model_fields))  # 23
    "

## Master-plan §8 progress

| Sub-step | Status |
|---|---|
| 8.0 scope proposal | ✅ |
| 8.1 `targeting.*` schema migrations | ✅ |
| 8.2 Deposit model loader | pending |
| 8.3 Athabasca uranium SME content | pending (Kyle) |
| 8.4 Target Recommendation Graph state + nodes | ✅ |
| 8.5 11 target agents skeletons | pending |
| 8.6 score_targets Hatchet workflow | pending |
| 8.7 Weighted scoring formula | pending (Kyle weights) |
| 8.8 SHAP-equivalent score factor writer | pending |
| 8.9 Sign-off ceremony | pending |
| 8.10 Target Pack map layer | pending |
| 8.11 Activepieces target workflows | pending |
| 8.12 Recommendation Explainer Agent | pending |
| 8.13 Acceptance test | pending |

**3 of 14 §8 sub-steps closed.** State + schema + scope locked.

## Recommended next tick

Doc-phase 87 = §8.5 (11 target agent skeletons) + §8.12 (Recommendation
Explainer). Batch all 11 in one tick — same pattern as doc-phase 81
(7 §7 agents in one tick).

After that: §8.6 (`score_targets` Hatchet workflow skeleton), §8.2
(deposit model loader skeleton).

## Carry-overs

Same as doc-phase 85.
