## Doc-phase 88 handoff — §8.6 score_targets workflow + §8.2 deposit-model loader

**Status:** Both ticks complete. Combined handoff.

## What landed

### §8.6 — score_targets Hatchet workflow

`src/fastapi/app/hatchet_workflows/score_targets.py` — wraps the
§18.2 Target Recommendation Graph. Same pattern as `generate_report`
(doc-phase 83):
- `ScoreTargetsInput` (7 fields): workspace_id, project_id,
  requested_by_user_id, aoi_geom_wkt, target_model_slug,
  scoring_kind, score_request_id.
- `ScoreTargetsOutput` (10 fields): run_id, success, zone counts,
  layer URIs, sent_to_review_cockpit + url, failure metadata.
- 24h execution_timeout (R5 sign-off pauses), retries=0.
- Body raises NotImplementedError until graph + langgraph extra
  graduate.

`worker.py` updated to import + register `score_targets` in the AI
pool (alongside `generate_report`). `worker --list` confirms both
workflows are visible.

### §8.2 — deposit-model loader + 10 templates

`src/fastapi/app/services/target_recommendation/deposit_models.py` —
10 deposit-model template stubs per §20.2:

| Slug | Display name | Commodity |
|---|---|---|
| athabasca_uranium | Athabasca Uranium (unconformity) | U |
| roll_front_uranium | Roll-Front Uranium (sandstone) | U |
| orogenic_gold | Orogenic Gold (lode) | Au |
| epithermal_gold | Epithermal Gold | Au |
| porphyry_copper | Porphyry Copper | Cu (+Mo, +Au) |
| vms | VMS | Cu (+Zn, +Pb, +Au, +Ag) |
| sedex | SEDEX | Pb (+Zn, +Ag) |
| lithium_pegmatite | Lithium Pegmatite | Li |
| oil_gas_basin | Oil/Gas Basin | oil (+gas) |
| custom | Custom (workspace-defined) | custom |

Public API:
- `DEPOSIT_MODEL_TEMPLATES: list[dict]` — all 10 in order
- `DEPOSIT_MODEL_BY_SLUG: dict[str, dict]` — lookup table
- `get_deposit_model_template(slug)` — returns a fresh copy

Each template seeds the §20.2 structure (host_rocks, structures,
alteration, geochemistry, geophysics, tectonic_setting,
positive/negative indicators, analogues, recommended_next_data) with
**empty placeholders**. Kyle's §8.3 pass populates these for
Athabasca uranium (the launch deposit model); other 9 stay skeletal
until SME data flows in.

### Smoke test

    docker exec georag-fastapi python -c "
        from app.services.target_recommendation import DEPOSIT_MODEL_TEMPLATES
        print(len(DEPOSIT_MODEL_TEMPLATES))  # 10
    "
    docker exec georag-fastapi python -m app.hatchet_workflows.worker --list \
        | grep score_targets
    # → score_targets

## Master-plan §8 progress

| Sub-step | Status |
|---|---|
| 8.0 scope proposal | ✅ |
| 8.1 `targeting.*` schema migrations | ✅ |
| 8.2 Deposit model loader + 10 templates | ✅ (empty stubs; SME content pending) |
| 8.3 Athabasca uranium SME content | pending (Kyle) |
| 8.4 Target Recommendation Graph | ✅ state + 12 node stubs |
| 8.5 11 target agents skeletons | ✅ |
| 8.6 score_targets Hatchet workflow | ✅ skeleton + registered |
| 8.7 Weighted scoring formula | pending (Kyle weights) |
| 8.8 SHAP-equivalent score factor writer | pending |
| 8.9 Sign-off ceremony glue | pending (Kyle decisions) |
| 8.10 Target Pack map layer | pending |
| 8.11 Activepieces target workflows | pending |
| 8.12 Recommendation Explainer Agent | ✅ (co-landed in §8.5) |
| 8.13 Acceptance test | pending |

**6 of 14 §8 sub-steps closed.** §8 backbone is now fully scaffolded
at skeleton/empty-template level. Remaining ticks need Kyle SME data
(8.3, 8.7, 8.9) or image rebuild (graph graduation).

## Carry-overs

1. **Unified image rebuild** stack — same blocker as §5 + §7 + §8.
2. **Kyle SME data**:
   - §8.3 — Athabasca uranium attributes (host rocks, alteration,
     geochemistry, geophysics, analogues)
   - §8.7 — per-factor weights for weighted scoring
   - §8.9 — QP credential verification mechanism choice
3. **Activepieces install** — gates §7.11 + §8.11 delivery workflows.

## Recommended next ticks

§8 autonomous-safe backend ticks are now functionally complete.
Remaining §8 work needs Kyle SME input or image rebuild.

Next phase candidates:
- **§9 (Geological Reasoning + Decision Intelligence)** — scope
  proposal would be the next autonomous-safe move. Master plan §20
  + §21.
- **§10 (Eval harness + Customer Support Cockpit)** — scope proposal.
  Less SME-dependent than §9; more infrastructure-heavy.
- **§11 (DR + deployment topologies + perf hardening)** — scope
  proposal. Pure ops/infra; no SME blockers.

Doc-phase 89 = open §9 scope proposal (highest leverage for an
autonomous run — §9 is where the system's "differentiating
intelligence layers" land).
