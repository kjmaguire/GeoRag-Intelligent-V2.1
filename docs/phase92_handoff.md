## Doc-phase 92 handoff — §9.9 + §9.10 decision intelligence schema + facade

**Status:** Complete. 5 tables live + 5 RLS policies + facade imports clean.

## What landed

### §9.9 — 5-table decision intelligence schema

`database/migrations/2026_05_13_130000_create_decision_intelligence_schema.php`:

| Table | Purpose |
|---|---|
| `silver.decision_records` | Core; one row per §21.3 decision (8 types) |
| `silver.decision_evidence_links` | Many-to-many decisions ↔ source chunks |
| `silver.decision_options` | Options considered (with `was_chosen` flag) |
| `silver.decision_outcomes` | Post-decision outcome tracking |
| `silver.decision_lessons_learned` | Retrospective captures |

CHECK constraints:
- `decision_type ∈ {target_recommendation, crs_decision, schema_mapping,
  public_data_import, export_approval, workflow_enablement,
  conflict_resolution, report_signoff}` — 8 types per §21.3
- `evidence_links.role ∈ {supporting, contradicting, context}`
- `uncertainty ∈ [0, 1]`

RLS on all 5 tables — workspace_id direct on decision_records;
EXISTS-on-parent for the other 4 (same pattern as
target_score_factors).

`hash` + `audit_ledger_id` columns on decision_records anchor each
decision to the hash chain via §22 emit_audit.

### §9.10 — record_decision facade

`src/fastapi/app/services/decision_intelligence/`:
- `recorder.py` — `record_decision()` async function. Single
  signature for all 8 decision types. Takes workspace_id +
  decision_type + recommendation + human_decision + decided_by_user_id
  + optional reason/uncertainty/evidence_chunk_ids/options_considered/
  outcome metadata. Returns decision_id UUID.
- `__init__.py` — re-exports.
- Skeleton (raises NotImplementedError); live implementation transacts
  the inserts + audit emission together.

This is the SINGLE entry point per §9.10 design recommendation (the
scope proposal flagged "scattered code" risk; one facade prevents it).

### Smoke test

    docker exec georag-fastapi python -c "
        from app.services.decision_intelligence import record_decision, DecisionType
        import typing
        print(len(typing.get_args(DecisionType)))  # 8
    "

## Master-plan §9 progress

| Sub-step | Status |
|---|---|
| 9.0 scope | ✅ |
| 9.1 ontology schema | ✅ |
| 9.2 ontology seeds | ✅ |
| 9.3 SME ontology population | pending (Kyle) |
| 9.4 hypotheses schema | ✅ |
| 9.5 hypothesis agent | ✅ skeleton |
| 9.6 spatial relationship engine | pending |
| 9.7 next-best-data recommendations | pending |
| 9.8 analogue finder | pending |
| 9.9 decision intelligence schema | ✅ |
| 9.10 decision capture facade | ✅ skeleton |
| 9.11 field_outcome_learning workflow | pending |
| 9.12 data lineage graph UI | pending (frontend) |
| 9.13 What Changed delta detection | pending |
| 9.14 acceptance test | pending |

**7 of 14 §9 sub-steps closed** (50%). The §9 backbone — ontology +
hypotheses + decision intelligence — is fully scaffolded.

## Recommended next tick

Doc-phase 93 = §9.11 (`field_outcome_learning` Hatchet workflow
skeleton) + §9.13 (What Changed delta detector skeleton). Both
backend; pattern matches `generate_report` + `score_targets`.

Alternative: doc-phase 93 = §9.6 + §9.7 + §9.8 (spatial relationship +
next-best-data + analogue finder agent skeletons). Three more agent
files in `app/agents/phase9/`.

Either viable. Workflow side is more concrete (registers in worker);
agents are more granular.

## Carry-overs

Same blockers as prior §9 ticks. Decision capture hooks (§9.10's
hook-into-existing-flows pass) still needed in 8 separate places —
will surface during integration tick.
