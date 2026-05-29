## Doc-phase 175 handoff — Nightly real-RAG cron emits regression alarm to audit ledger

**Status:** Live + 6/6 nightly cron tests pass + alarm row verified end-to-end against audit.audit_ledger + 112/112 substrate verifier preserved.

## What landed

When the `eval_real_rag_nightly` cron returns `success=False` (one or
more regressions detected against the §10.6 promotion-gate baseline),
the workflow now **emits an `eval.regression_detected` row to
`audit.audit_ledger`** before returning.

Downstream notification flows (Activepieces webhooks polling the
audit ledger by `action_type`) pick the alarm up from there.

### Why audit-ledger emission, not direct Slack/PagerDuty?

The audit ledger is the canonical notification surface in this codebase:
- Already tamper-evident (hash chain)
- Already partition-indexed for time-series queries
- Already the integration point for `external_notification` flows
- Adding direct outbound webhook plumbing would create a second
  notification path that duplicates the audit emission anyway

Emitting once to the audit ledger keeps the alarm in the same
tamper-evident record as everything else the platform does, and lets
Activepieces / operator tooling subscribe via the same polling
mechanism it already uses for `external_notification.received`.

### Audit row shape

```python
action_type     = "eval.regression_detected"
actor_kind      = "workflow"
target_schema   = "eval"
target_table    = "run_summaries"
target_id       = str(eval_run_id)
payload         = {
    "eval_run_id":           "<uuid>",
    "eval_request_id":       "<uuid>",
    "evaluator_kind":        "real_rag_v1",
    "question_set_filter":   "refusal_correctness",
    "question_count":        8,
    "fail_count":            2,
    "regression_count":      2,
    "promotion_blocked":     true,
    "failure_summary":       "2 regressions on layer 5_chunk_provenance",
    "cron_origin":           "eval_real_rag_nightly",
    "doc_phase":             175,
}
trace_id        = ctx.workflow_run_id  # links back to Hatchet workflow run
```

### Output shape extension

`EvalRealRagNightlyOutput.regression_audit_id: str | None = None` — populated
when an alarm fires, so observability tooling can correlate the cron
output back to the audit row without re-querying.

### Failure-mode behavior

The alarm emit is wrapped in try/except. If audit emission fails (DB
hiccup, partition missing, encryption-key unconfigured), the cron's
workflow output still carries the regression signal — the audit
emission is supplementary, not the primary signal of record.

### Subscriber pattern (for Activepieces / operator side)

```sql
-- Poll for alarms in the last 5 min, not yet handled
SELECT id, target_id AS eval_run_id, payload, created_at, trace_id
  FROM audit.audit_ledger
 WHERE action_type = 'eval.regression_detected'
   AND created_at >= now() - interval '5 minutes'
   AND NOT EXISTS (
       SELECT 1 FROM audit.audit_ledger ack
        WHERE ack.action_type = 'eval.regression_alarm_acknowledged'
          AND ack.target_id = audit.audit_ledger.target_id::text
   )
 ORDER BY created_at DESC;
```

Activepieces can fan this out to Slack / PagerDuty / email per its
own configured destinations; the application doesn't need to know.

## Tests — 2 new + 4 prior = 6/6

`tests/test_eval_real_rag_nightly_workflow.py`:

| Case | Verifies |
|---|---|
| `test_workflow_alarm_helper_emits_audit_row` | `_emit_regression_alarm` writes the audit row with `eval.regression_detected` + full payload shape (verified by SELECT against the live DB) |
| `test_workflow_does_not_emit_alarm_when_success` | Green-state cron run leaves `regression_audit_id=None` — no noise in the audit ledger on clean runs |

Existing 4 tests continue to pass; the manual-override empty-set
path now also asserts `regression_audit_id is None`.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest \
    tests/test_eval_real_rag_nightly_workflow.py -v
# → 6 passed in 69.47s (4 prior + 2 new)

bash scripts/autonomous_run_substrate_verify.sh
# → 112/112 checks passed
```

## Cumulative session state — 43 ticks closed

- **Doc-phase ticks this run:** **43** (132 → 175)
- **Substrate verifier:** **112/112 PASS**
- **Live pytest cases:** 286 (was 284 — +2 alarm-emission tests)
- **Laravel Track3 dashboard tests:** 14/14 PASS
- **§10.6 promotion-gate alarm loop:** ALARM EMITS — operator subscription pending
- **Hatchet AI pool workflows:** 12
- **Phase A ingestion:** staging at 22% (200GB → container-local, ETA ~25 min)

## What's next

The alarm-loop's first half is closed (workflow emits to audit
ledger). The second half is operator subscription:

- **Activepieces flow** subscribing to `action_type='eval.regression_detected'`
  audit rows. Configure once in the Activepieces UI; no application
  code change needed.
- **`eval.regression_alarm_acknowledged`** action_type for the
  ack/dismiss flow once an operator has triaged. Trivial follow-on
  audit emission from the dashboard "acknowledge alarm" button.

## Carry-overs

- The alarm payload carries `failure_summary` as a free-text field.
  Future graduations could structure this further (e.g., the
  failure-layer breakdown counts) so downstream tooling doesn't
  string-parse. Today the free text is sufficient because the audit
  row's `payload.failure_summary` is rendered as-is in Activepieces
  Slack templates.
- The alarm emits at workflow-completion time (after the cron's
  evaluation finishes). For long-running evals this means alarm
  arrival lags failure-occurrence by minutes. If sub-minute alarm
  latency becomes a requirement, the evaluator would need to emit
  per-question alarms — significant scope change.
- An "alarm acknowledged" audit action_type isn't yet defined. The
  current subscriber pattern (sample SQL above) uses
  `NOT EXISTS (... ack ...)` against a future ack row that doesn't
  exist yet, so today Activepieces would re-notify on every poll
  until the ack pattern lands.
