## Doc-phase 102 handoff — §12.7 + §12.8 + §12.9 + §12.10 extended §12 scaffolding

**Status:** Complete. 4 more §12 sub-steps closed. **§12 now at 9/13
(69%).** Worker pool runs **10 long-running AI workflows**.

## What landed

### §12.7 — source trust scoring infrastructure

- **2 new tables**: `silver.source_trust_scores` +
  `silver.source_trust_features`. CHECK enum on feature_name
  restricts to the §21.5 list (citation_accuracy,
  claim_ledger_consistency, recency, document_type,
  author_issuer_reputation). RLS workspace-scoped.
- **`train_source_trust` Hatchet workflow** — trains per-workspace
  source-trust XGBoost on accumulated workspace state. 2h timeout;
  retries=0. Registered in AI pool.

### §12.8 — source trust → retrieval ranking extension

`src/fastapi/app/services/source_trust/`:
- `boost.py` — `boost_by_trust(conn, *, workspace_id,
  retrieved_chunks, boost_weight, fallback_trust)` — post-fusion
  adjustment layer. Multiplicative boost keeps fusion primary;
  trust modulates rather than dominates. Skeleton.

Schema-compatible with existing `app.services.fusion.combine_scores`
API — callers add this as a post-fusion call.

### §12.9 — LLM Incident Diagnosis Graph

`src/fastapi/app/services/llm_incident_diagnosis/`:
- `state.py` — `IncidentDiagnosisState` (19 fields). `IncidentKind`
  Literal enum: hallucination, refusal_failure, citation_drift,
  numeric_grounding_failure, tone_template_violation, cost_spike,
  latency_spike, other.
- `nodes.py` — 5 async node stubs: `classify_incident →
  gather_traces → identify_root_cause → propose_remediation →
  record_diagnosis`. LangGraph wiring waits for image rebuild.
- The existing phase0 LLM Incident Diagnosis agent stub graduates
  here when langgraph lands.

### §12.10 — continuous learning loop

`src/fastapi/app/hatchet_workflows/continuous_learning_loop.py`:
- Daily cron orchestrator (Hatchet workflow) that triggers
  `train_target_model` + `train_source_trust` based on per-
  workspace delta thresholds, then runs `evaluate_workspace` on
  active workspaces.
- 8h execution_timeout; retries=0.
- Default thresholds: +25 new target_outcomes per deposit model
  triggers target retraining; +500 new citations per workspace
  triggers source-trust retraining.

This is the §20.8 "geological learning loop" anchor — every drilled
target + every cited claim feeds back into the models.

### Worker pool — 10 workflows registered

`worker --list` now confirms:

    outbox_dispatcher
    ingest_pdf
    re_ocr_page
    (3 ingestion agent workflows)
    audit_ledger_verify
    phase2_smoke
    public_geoscience_pull
    external_notification
    flow_jwt_key_reaper
    mv_refresh_silver
    generate_report                  (§7.10, doc-phase 83)
    score_targets                    (§8.6, doc-phase 88)
    field_outcome_learning           (§9.11, doc-phase 94)
    what_changed_detector            (§9.13, doc-phase 94)
    evaluate_workspace               (§10.4, doc-phase 98)
    support_replay                   (§10.10, doc-phase 98)
    restore_workspace                (§11.3, doc-phase 100)
    train_target_model               (§12.3, doc-phase 101)
    train_source_trust               (§12.7, doc-phase 102)
    continuous_learning_loop         (§12.10, doc-phase 102)
    (7 AI agent workflows)

10 net-new long-running AI workflows added this autonomous run.

## Master-plan §12 progress

| Sub-step | Status |
|---|---|
| 12.0 scope proposal | ✅ |
| 12.1 image rebuild deps | pending |
| 12.2 scoring_kind enum | ✅ (done in §8.1) |
| 12.3 train_target_model workflow | ✅ |
| 12.4 XGBoost inference branch | ✅ |
| 12.5 SHAP factor writer | ✅ |
| 12.6 A/B comparison (ensemble) | pending |
| 12.7 source trust scoring | ✅ schema + workflow |
| 12.8 source trust → retrieval ranking | ✅ skeleton |
| 12.9 LLM Incident Diagnosis Graph | ✅ state + 5 node stubs |
| 12.10 continuous learning loop cron | ✅ skeleton |
| 12.11 Storage Tiering Agent graduation | pending (existing skeleton) |
| 12.12 Lineage Reporter Agent graduation | pending (existing skeleton) |
| 12.13 acceptance | pending (waits on data) |

**9 of 13 §12 sub-steps closed (69%).** Remaining: image rebuild
(12.1) + A/B comparison logic (12.6) + 2 agent graduations (12.11,
12.12) + acceptance (12.13).

## Recommended next tick

Doc-phase 103 = §12.6 A/B comparison logic + §12.11/12.12 graduations
of existing Storage Tiering + Lineage Reporter agents (which were
stubbed in earlier phase0 work).

After that, §12 is fully scaffolded at autonomous-safe level. Then
the master-plan substrate would be truly + completely done.

## Carry-overs

Same as prior. Plus the existing phase0 Storage Tiering + Lineage
Reporter agents need to graduate from their phase0 skeleton state
in §12.11 + §12.12.
