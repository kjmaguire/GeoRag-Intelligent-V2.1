## Doc-phase 97 handoff — §10.1 + §10.5 + §10.8 eval + ops schemas

**Status:** Complete. 6 new tables across 2 new schemas; both verified.

## What landed

### §10.1 + §10.5 — eval.* schema (3 tables)

`database/migrations/2026_05_13_140000_create_eval_schema.php`:

| Table | Purpose |
|---|---|
| `eval.golden_questions` | Per-question definition (text, context_setup, expected_*, difficulty, status) |
| `eval.run_results` | One row per question per eval run; pass/fail + failure layer + latency/tokens |
| `eval.run_summaries` | Aggregate per-run pass/fail/regression + promotion-blocking + override metadata |

CHECK enums:
- `question_set ∈ {core_chat, public_private_boundary, numeric_grounding,
  refusal_correctness, target_recommendation, report_section,
  schema_mapping, ocr_triage}` (8 sets per §24.1)
- `difficulty ∈ {easy, medium, hard}`
- `status ∈ {draft, active, retired}`
- `triggered_by ∈ {cron, manual, promotion_gate, prompt_change}`
- `pass_count + fail_count ≤ question_count`

No RLS — eval is GLOBAL operational data.

### §10.8 — ops.* schema (3 tables)

`database/migrations/2026_05_13_140100_create_ops_support_schema.php`:

| Table | Purpose |
|---|---|
| `ops.support_tickets` | Customer-reported issues with workspace_id (nullable), channel, category, severity, status |
| `ops.support_ticket_traces` | Many-to-many: tickets ↔ correlated trace_ids |
| `ops.support_replay_runs` | Workflow replay attempts (dry_run by default) for diagnosis |

CHECK enums:
- `channel ∈ {in_app, email, webhook, phone}`
- `category ∈ {wrong_answer, failed_ingestion, failed_report, integration_issue, performance, other}`
- `severity ∈ {low, medium, high, critical}`
- `status ∈ {open, investigating, resolved, closed}` (tickets)
- `status ∈ {pending, running, completed, failed, aborted}` (replays)

No RLS — `ops.*` is global per §25.3; cross-workspace access logged
via `audit_ledger.action_type = 'support_access'`. Application-level
ops-role enforcement gates the cockpit UI.

Both schemas applied via superuser `georag` (same pattern as §6.5,
§8.1, §9.1, §9.4, §9.9).

## Master-plan §10 progress

| Sub-step | Status |
|---|---|
| 10.0 scope proposal | ✅ |
| 10.1 eval.golden_questions schema | ✅ |
| 10.2 golden questions seed loader | pending |
| 10.3 question authoring UI | pending (frontend; Kyle) |
| 10.4 evaluate_workspace Hatchet workflow | pending (next tick) |
| 10.5 eval result schemas | ✅ |
| 10.6 regression threshold enforcer | pending |
| 10.7 Eval Dashboard | pending (frontend; Kyle) |
| 10.8 ops.* support schema | ✅ |
| 10.9 5 support agents skeletons | pending (next tick) |
| 10.10 support_replay Hatchet workflow | pending |
| 10.11 Customer Support Cockpit UI | pending (frontend; Kyle) |
| 10.12 cross-workspace access audit emission | pending |
| 10.13 LangFuse trace replay link | pending |
| 10.14 acceptance test | pending |

**3 of 14 §10 sub-steps closed.**

## Recommended next tick

Doc-phase 98 = §10.4 (`evaluate_workspace` Hatchet workflow skeleton)
+ §10.9 (5 support agent skeletons) + §10.10 (`support_replay`
Hatchet workflow skeleton). Batches 3 small sub-steps into one tick.

Pattern matches doc-phase 81 (7 §7 agents in one tick) and doc-phase
83 (workflow skeleton + worker registration).

## Carry-overs

Same as prior ticks — image rebuild, Kyle SME content, Activepieces,
frontend pass. Plus new ops-role gating decisions for §10.11 cockpit
when frontend work begins.
