## Doc-phase 98 handoff — §10.4 + §10.9 + §10.10 eval workflow + 5 support agents + replay workflow

**Status:** Complete. 5 agent skeletons + 2 Hatchet workflows registered + verified.

## What landed

### §10.4 — evaluate_workspace Hatchet workflow

`src/fastapi/app/hatchet_workflows/evaluate_workspace.py`:
- `EvaluateWorkspaceInput` (5 fields): triggered_by, trigger_payload,
  question_set_filter, blocks_promotion, eval_request_id.
- `EvaluateWorkspaceOutput` (7 fields): run_id, success, question_count,
  pass_count, fail_count, regression_count, promotion_blocked,
  failure_summary.
- 2h execution_timeout; retries=0. Skeleton.

### §10.10 — support_replay Hatchet workflow

`src/fastapi/app/hatchet_workflows/support_replay.py`:
- `SupportReplayInput` (5 fields): ticket_id, original_workflow_run_id,
  initiated_by_user_id, dry_run (default true), replay_request_id.
- `SupportReplayOutput` (5 fields): replay_id, success, diff_summary,
  replay_workflow_run_id, error.
- 1h execution_timeout; retries=0. Skeleton.

### §10.9 — 5 support agents

New package `app/agents/phase10/`:

| Agent | File | Role | Risk |
|---|---|---|---|
| Ticket Triage | `ticket_triage.py` | LangGraph cognition — categorize + severity + dupes | R1 |
| Root Cause Investigation | `root_cause_investigation.py` | LangGraph — hypothesis drafting from traces | R1 |
| Support Packet | `support_packet.py` | Activepieces — diagnostic bundle to SeaweedFS | R2 |
| Customer Response Drafting | `customer_response_drafting.py` | Activepieces — drafts response | R1 |
| Escalation Routing | `escalation_routing.py` | Activepieces — high-severity routing | R2 |

Each `@georag_agent`-decorated; signatures + docstrings lock the
contract; bodies raise NotImplementedError.

### Worker registration

Both workflows added to the AI pool in `worker.py`. `worker --list`
confirms 6 long-running AI workflows now registered:

    generate_report
    score_targets
    field_outcome_learning
    what_changed_detector
    evaluate_workspace
    support_replay

## Master-plan §10 progress

| Sub-step | Status |
|---|---|
| 10.0 scope proposal | ✅ |
| 10.1 eval.golden_questions schema | ✅ |
| 10.2 golden questions seed loader | pending |
| 10.3 question authoring UI | pending (frontend) |
| 10.4 evaluate_workspace workflow | ✅ skeleton |
| 10.5 eval result schemas | ✅ |
| 10.6 regression threshold enforcer | pending |
| 10.7 Eval Dashboard | pending (frontend) |
| 10.8 ops.* support schema | ✅ |
| 10.9 5 support agents skeletons | ✅ |
| 10.10 support_replay workflow | ✅ skeleton |
| 10.11 Customer Support Cockpit UI | pending (frontend) |
| 10.12 cross-workspace access audit emission | pending |
| 10.13 LangFuse trace replay link | pending |
| 10.14 acceptance test | pending |

**7 of 14 §10 sub-steps closed (50%)** — backend backbone scaffolded.
Remaining 7 ticks: 2 frontend (10.3, 10.7, 10.11), 2 small backend
(10.6, 10.12, 10.13), 1 seed loader (10.2), 1 acceptance.

## Recommended next tick

Doc-phase 99 = §10.2 golden-question seed loader skeleton + §10.6
regression threshold config / enforcer skeleton + §10.12 cross-
workspace access audit-emission utility. Three small backend pieces
that close most remaining §10 autonomous-safe work.

After that, §10 is functionally complete at scaffold level — only
frontend (3 sub-steps) + acceptance (10.14) remain.

## Carry-overs

Same as prior. Plus:
- Golden-question content (§10.2 SME pass) — 100 questions across 8
  sets per master plan. ~50% mechanical (OCR triage, schema mapping)
  + ~50% SME (core_chat, target_recommendation, etc.).
