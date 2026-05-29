## Doc-phase 99 handoff — §10.2 + §10.6 + §10.12 three small skeletons

**Status:** Complete. 3 service-utility skeletons; all import clean.

## What landed

### §10.2 — golden-question seed loader

`src/fastapi/app/services/eval/seeds.py`:
- `QuestionSet` Literal — 8 canonical sets per §24.1
- `QUESTION_SET_NOTES` — per-set SME guidance (which sets are
  mechanical vs SME-authored; estimate ~50/50 split per the §10
  scope proposal)
- `QUESTION_SET_SLOTS: dict[QuestionSet, list[dict]]` — empty list
  per set, ready for §10.2 population pass
- `seed_question_sets()` — list the 8 canonical set names

### §10.6 — regression-threshold config + promotion gate

`src/fastapi/app/services/eval/thresholds.py`:
- `RegressionThresholds` Pydantic model:
  - `max_absolute_fail_count` (default 5)
  - `max_regression_count` (default 2)
  - `per_set_max_regression` defaults — **`public_private_boundary`
    + `target_recommendation` both at 0 (zero-tolerance)**;
    `core_chat`/`numeric_grounding`/`refusal_correctness` at 1;
    `report_section`/`schema_mapping` at 2; `ocr_triage` at 3.
  - `mode = "warning_only"` default per Kyle's tabled
    open-question recommendation (2-week ramp then flip to
    blocking).
- `DEFAULT_REGRESSION_THRESHOLDS` constant.
- `check_promotion_gate()` async function — skeleton; returns
  `{blocks_promotion, reasons[], mode}`.

The zero-tolerance settings for `public_private_boundary` +
`target_recommendation` encode the §2.9 + §18.1 regulatory anchors —
any regression there is a blocker.

### §10.12 — cross-workspace access audit emission

`src/fastapi/app/services/support_cockpit/access_audit.py`:
- `emit_support_access_audit(conn, *, workspace_id, ops_user_id,
  ticket_id, access_kind, target_summary, payload)` — emits an
  `audit_ledger.action_type='support_access'` entry. Returns
  AuditLedgerEntry.
- Access-kind vocabulary documented in docstring:
  `workspace_state_view`, `audit_ledger_excerpt`,
  `workflow_replay_dry_run`, `workflow_replay_live`,
  `langfuse_trace_read`.

Wraps the existing `app.audit.emit_audit` pattern (same one used
for regular state-changing flows). Workspace owners surface these
on their own audit-ledger view per §25.3.

## Master-plan §10 progress

| Sub-step | Status |
|---|---|
| 10.0 scope proposal | ✅ |
| 10.1 eval.golden_questions schema | ✅ |
| 10.2 golden questions seed loader | ✅ skeleton |
| 10.3 question authoring UI | pending (frontend) |
| 10.4 evaluate_workspace workflow | ✅ skeleton |
| 10.5 eval result schemas | ✅ |
| 10.6 regression threshold enforcer | ✅ skeleton |
| 10.7 Eval Dashboard | pending (frontend) |
| 10.8 ops.* support schema | ✅ |
| 10.9 5 support agents skeletons | ✅ |
| 10.10 support_replay workflow | ✅ skeleton |
| 10.11 Customer Support Cockpit UI | pending (frontend) |
| 10.12 cross-workspace access audit | ✅ skeleton |
| 10.13 LangFuse trace replay link | pending (small) |
| 10.14 acceptance test | pending |

**10 of 14 §10 sub-steps closed (71%)** — §10 backend is essentially
done at scaffold level. Remaining: 3 frontend (10.3, 10.7, 10.11),
1 small backend (10.13 LangFuse link), 1 acceptance (10.14).

## Recommended next tick

Doc-phase 100 = either:
- **§11.3 + §11.10** (small autonomous-safe §11 skeletons —
  `restore_workspace` workflow + audit archival logic), OR
- **§12.3 + §12.4 + §12.5** (XGBoost training workflow + inference
  branch + SHAP writer skeletons)

§11 is purely workflow + utility skeletons; §12 is also workflow
+ ML-utility skeletons. Both ~2-3 ticks each.

§11 next (smaller, closes §11's autonomous slice in one tick) then
§12 to wrap up the full master-plan autonomous-safe scaffolding.

## Carry-overs

Unchanged plus:
- Golden-question SME content (§10.2 population pass) — needs Kyle's
  call on ownership.
- §10 acceptance test (10.14) waits for the runner + threshold gate
  to graduate.
