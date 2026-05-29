## Doc-phase 101 handoff — §12.3 + §12.4 + §12.5 XGBoost scaffolding (FINAL autonomous-safe master-plan tick)

**Status:** Complete. **The master-plan autonomous-safe substrate is now
COMPLETE.** Every §3-§12 backend skeleton that can land without Kyle
input, image rebuild, or frontend work has landed.

## What landed

### §12.3 — train_target_model Hatchet workflow

`src/fastapi/app/hatchet_workflows/train_target_model.py`:
- `TrainTargetModelInput` (5 fields): target_model_id,
  initiated_by_user_id, min_outcomes_per_deposit_model (default 50),
  use_synthetic_outcomes, activate_on_success, train_request_id.
- `TrainTargetModelOutput` (6 fields): success, new_version_id,
  outcomes_used, training_metrics, activated, failure_reason.
- 4h execution_timeout; retries=0.
- Refuses to train below threshold (raises NotEnoughDataError).

Registered in worker AI pool. `worker --list` now shows **8
long-running AI workflows**: generate_report, score_targets,
field_outcome_learning, what_changed_detector, evaluate_workspace,
support_replay, restore_workspace, train_target_model.

### §12.4 — XGBoost inference branch

`src/fastapi/app/services/target_scoring_ml/xgboost_inference.py`:
- `XGBoostInferenceResult` dataclass (zone_id, aggregate_score,
  feature_values, shap_attributions).
- `score_zone_xgboost(conn, *, zone_id, model_version_id,
  feature_payload)` async function.

Sits as the alternate branch in the §8.5 `target_scoring` agent —
when `scoring_kind=='xgboost'`, the agent loads the serialized
model + runs inference per zone. Weighted branch unchanged.

### §12.5 — SHAP factor writer

`src/fastapi/app/services/target_scoring_ml/shap_writer.py`:
- `write_shap_factors(conn, *, score_id, shap_attributions,
  feature_values, evidence_chunk_id_lookup)` async function.

Writes per-SHAP-feature rows into `targeting.target_score_factors` —
schema-compatible with weighted-path's deterministic decomposition.
Downstream consumers (Report Builder per-target briefs) don't branch
on scoring_kind. Enforces the §18.3 "no black-box targeting" rule.

## Master-plan §12 progress

| Sub-step | Status |
|---|---|
| 12.0 scope proposal | ✅ |
| 12.1 image rebuild deps | pending (Kyle + image rebuild) |
| 12.2 scoring_kind enum already supports xgboost | ✅ (done in §8.1) |
| 12.3 train_target_model workflow | ✅ skeleton |
| 12.4 XGBoost inference branch | ✅ skeleton |
| 12.5 SHAP-equivalent factor writer | ✅ skeleton |
| 12.6 A/B comparison (ensemble scoring_kind) | pending |
| 12.7 source trust scoring | pending |
| 12.8 source trust → retrieval ranking | pending |
| 12.9 LLM Incident Diagnosis Graph | pending |
| 12.10 continuous learning loop crons | pending |
| 12.11 Storage Tiering Agent graduation | pending |
| 12.12 Lineage Reporter Agent graduation | pending |
| 12.13 acceptance | pending (waits on data) |

**5 of 13 §12 sub-steps closed (38%).** Remaining ticks need image
rebuild (12.1) + multi-month outcome accumulation (12.6+).

---

## 🏁 MASTER PLAN AUTONOMOUS-SAFE SCAFFOLDING COMPLETE

Doc-phases 74-101 (this run continuation) pushed the master plan
from "v1.49 baseline with §3 closing" to:

| Phase | Final autonomous state |
|---|---|
| §3 §04p PDF stack + OCR quality | Steps 1-8 functionally done; Step 9-10 blocked on SME |
| §4 RAG/Answer Graph | v1.49 baseline + R-P11 done |
| §5 Spatial pipeline + drillhole visuals | scope proposed; 3 gold tables + 2 agent skeletons |
| §6 PublicGeo + MapLibre | scope proposed; 3 sub-steps closed (audit + boundary agent + saved map views) |
| §7 Reporting + dashboards | scope proposed; 10/16 sub-steps closed |
| §8 Target Recommendation Engine | scope proposed; 6/14 sub-steps closed |
| §9 Geological Reasoning + Decision Intelligence | scope proposed; **12/14 (86%)** sub-steps closed |
| §10 Eval harness + Support Cockpit | scope proposed; **10/14 (71%)** sub-steps closed |
| §11 DR + perf hardening | scope proposed; **3/12** sub-steps closed (autonomous-safe slice done) |
| §12 XGBoost + advanced learning | scope proposed; 5/13 sub-steps closed |

Cumulative autonomous-run deliverables (doc-phases 74-101 continuation):
- **7 new scope-proposal docs** (§6/§7/§8/§9/§10/§11/§12)
- **28 doc-phase handoff docs** (74-101)
- **2 master continuation briefings** (overnight + this continuation)
- **24 new database tables** across 4 new schemas:
  - silver.saved_map_views (1)
  - targeting.* (10 tables)
  - silver.geological_ontology_terms + _synonyms (2)
  - silver.hypotheses + hypothesis_evidence_links (2)
  - silver.decision_records + 4 related (5)
  - eval.* (3)
  - ops.* (3)
- **39+ agent skeletons** across phase6/7/8/9/10 packages
- **2 LangGraph state models + 25 node stubs**
- **8 Hatchet workflows registered** in AI pool
- **9 new service packages**: report_builder, target_recommendation,
  decision_intelligence, geological_ontology, audit/hash_chain_proof,
  audit/cold_tier_archive, eval, support_cockpit, target_scoring_ml,
  plus app.agents.phase6/7/8/9/10 packages

**All work import-smoke-tested in the running fastapi container.
All Hatchet workflows visible via worker --list. All new database
tables verified via \d queries. RLS policies in place where
appropriate.**

---

## What's left to do (master-plan completion)

Per the §12 scope proposal closing section, remaining work splits
into **three tracks**:

### Track 1: Image rebuild bundle (one rebuild + container restart)

Unblocks §5 viz endpoints + §7/§8/§9 graph wiring + §12 ML deps.
Single bundled rebuild brings in:
- geopandas + rasterio + mplstereonet (§5)
- weasyprint + python-docx + openpyxl (§7.9)
- langgraph + langgraph-checkpoint-postgres +
  langchain-mcp-adapters (already a pyproject extra) for §7.1, §8.4,
  §9.5 graph wiring
- xgboost + shap + scikit-learn (§12.1)

### Track 2: Kyle SME content

- §8.3 Athabasca uranium model attributes (host rocks, alteration,
  geochemistry, geophysics, analogues)
- §8.7 weighted scoring per-factor weights
- §8.9 QP credential verification mechanism
- §9.3 geological ontology population (~1100 entries across 12
  classes; estimated multi-week external contractor pass)
- §10.2 golden-question content (~100 questions across 8 sets;
  ~50% SME)
- §11 DR runbooks + deployment topology choices
- §3 Step 9-10 (50-PDF corpus labeling + RAGFlow retirement)

### Track 3: Frontend pass

- §6.7-§6.14 MapLibre layer packs + Feature Inspector + AOI tools +
  Evidence Map Mode
- §7.12-§7.15 22 dashboards (product + workflow + ops tiers)
- §8.10 Target Pack map layer
- §9.12 Data lineage graph UI (React Flow)
- §10.3 question authoring UI
- §10.7 Eval Dashboard
- §10.11 Customer Support Cockpit UI

All product-feel decisions (color schemes, interaction handlers,
layout density). Waits for Kyle.

Each track can proceed independently once Kyle has bandwidth.

## Carry-overs

Same blockers as prior. The autonomous run has exhausted backend-
only autonomous-safe work for §3-§12. Next session pickup
will be either:
- Kyle SME content pass (§8.3, §9.3, §10.2)
- Image rebuild + skeleton graduation pass (the big unlock)
- Frontend pass start (with Kyle in the loop)
