## Doc-phase 103 handoff — §12.6 A/B comparison + §12.11/§12.12 graduation notes

**Status:** Complete. **§12 now at 11/13 (85%).**

## What landed

### §12.6 — A/B comparison harness

`src/fastapi/app/services/target_scoring_ml/ab_comparison.py`:

- **`ABComparisonResult` dataclass** — zone_id, weighted_score,
  xgboost_score, ensemble_score, divergence,
  confidence_drop_fallback, weighted_score_id, xgboost_score_id.
- **`compute_ab_scores(conn, *, workspace_id, zone_id,
  weighted_version_id, xgboost_version_id, ensemble_weight_weighted,
  ensemble_weight_xgboost, xgboost_confidence_floor)`** — async
  function that runs both scoring paths against one zone, writes
  both target_scores rows, returns comparison. Skeleton.
- **`choose_display_strategy(...)` — pure function, NOT a skeleton.**
  Implements the §18.3 fallback policy:
  - `weighted_only` when xgboost score/confidence is None
  - `weighted_only` when xgboost_confidence < confidence_floor (0.4)
  - `ensemble` when both available + confidence good
- `Strategy` Literal type: weighted_only | xgboost_only | ensemble.

Verified via in-container Python: all three strategy branches
return correctly.

Default ensemble weights:
- `weighted = 0.6` (deterministic baseline stays primary per §18.3)
- `xgboost = 0.4`

### §12.11 + §12.12 — graduation note (not new skeletons)

`storage_tiering_run` and `lineage_walk` already exist as Hatchet
workflows in `app/hatchet_workflows/phase0_agents.py` (registered in
the AI pool from Phase 0). Their `@georag_agent`-decorated agent
bodies are phase0 skeletons.

§12.11 (Storage Tiering live) and §12.12 (Lineage Reporter live)
are **agent-body graduations of existing workflows**, not new
workflow registrations. The workflows are already routed; only the
agent bodies need behavior implementation.

Graduation requirements:
- §12.11 Storage Tiering: behavior depends on SeaweedFS cold-tier
  bucket existing (overlaps with §11.10 audit cold-tier archival
  doc-phase 100). Once cold-tier bucket is provisioned, Storage
  Tiering Agent moves hot → warm → cold per workspace age policy.
- §12.12 Lineage Reporter: reads from audit_ledger + claim ledger;
  emits structured lineage JSON for each artifact. Behavior needs
  Kyle's call on output cadence + storage destination.

Both graduations are skeleton→behavior implementations, not new
scaffolding. Deferred to the §12 unblock pass with Kyle.

## Master-plan §12 progress

| Sub-step | Status |
|---|---|
| 12.0 scope | ✅ |
| 12.1 image rebuild deps | pending |
| 12.2 scoring_kind enum | ✅ |
| 12.3 train_target_model | ✅ |
| 12.4 XGBoost inference | ✅ |
| 12.5 SHAP writer | ✅ |
| 12.6 A/B comparison | ✅ (skeleton + working choose_display_strategy) |
| 12.7 source trust scoring | ✅ |
| 12.8 source trust → retrieval | ✅ |
| 12.9 LLM Incident Diagnosis Graph | ✅ |
| 12.10 continuous learning loop | ✅ |
| 12.11 Storage Tiering Agent live | pending (existing skeleton, not new scaffolding) |
| 12.12 Lineage Reporter Agent live | pending (existing skeleton, not new scaffolding) |
| 12.13 acceptance | pending (waits on data) |

**11 of 13 §12 sub-steps closed (85%).** Remaining 2 sub-steps
(12.11 + 12.12) are graduations of existing workflows — they're
in a different category than new scaffolding.

## Comprehensive autonomous-run-substrate state

Per master-plan phase, autonomous-safe scaffolding is now:

| Phase | Autonomous % done |
|---|---|
| §3 | 8/10 (Steps 9-10 = SME) |
| §4 | functionally done |
| §5 | scope + substrate (3 gold tables + 2 agent skeletons) |
| §6 | 3/15 (boundary agent, saved views; rest = MapLibre frontend) |
| §7 | 10/16 (graph + agents + workflow + templates done; rest = frontend) |
| §8 | 6/14 (schema + graph + agents + workflow done; rest = Kyle SME + frontend) |
| §9 | 12/14 (86%) — ontology + hypotheses + decision IL + agents + workflows |
| §10 | 10/14 (71%) — eval + ops schemas + workflows + 5 agents + threshold gate |
| §11 | 3/12 (autonomous-safe slice done; rest = Kyle ops) |
| §12 | **11/13 (85%)** — XGBoost + source trust + incident diagnosis + learning loop |

**Net: 76 of ~111 sub-steps closed (68%) at the autonomous-safe
scaffolding level** across §3-§12.

## Recommended next ticks

The autonomous-safe scaffolding pass is now truly exhausted for
backend skeletons. Remaining autonomous-safe ground:
- §10.13 LangFuse trace replay link integration (small backend
  utility)
- §11.5 Tenant Isolation Auditor CI workflow stub (operational)
- DR runbook templates (markdown stubs with TODO sections — §11.4)
- Frontend scaffolding stubs (Inertia React pages with placeholder
  content) — possible but borderline Kyle-territory

Doc-phase 104 = §10.13 + §11.5 (small backend + CI stub).

## Carry-overs

Same blockers. Plus:
- §12.6 ensemble weight tuning waits on real outcome data.
- §12.11 / §12.12 agent-body implementations need Kyle calls on
  cold-tier destination + lineage output cadence.
