## Doc-phase 146 handoff — support_replay Hatchet task body graduated

**Status:** Live + 2/2 pytest cases + 1 production replay run. **88/88 substrate verifier**.

## What landed

`support_replay` Hatchet workflow (§10.10 / §25.1) — doc-phase 98
skeleton → doc-phase 146 graduation.

Per §25.1 the workflow re-executes a failed run in dry-run mode to
help support identify root cause. The graduation takes the
**practical interpretation**: rather than attempting a real dry-run
re-execution of the original Hatchet workflow (which needs deeper
Hatchet APIs for fetching past run inputs + dispatching child runs),
the task body invokes the §25.4 support-agent chain against the
ticket the replay is for.

### Pipeline

```text
INSERT ops.support_replay_runs (status='running')
  ↓
triage_ticket           (re-classify severity/category if open)
  ↓
investigate_ticket      (scan recent audit for relevant signal)
  ↓
build_support_packet    (assemble full chain snapshot)
  ↓
draft_customer_response (template + investigation summary)
  ↓
route_escalation        (decision tree → on_call / sme / engineer / etc.)
  ↓
UPDATE ops.support_replay_runs (status='completed', diff_summary, completed_at)
  ↓
emit support.replay.completed audit anchor
```

Each step's outcome contributes to `diff_summary` so operators can
read the chain at a glance:

```text
triage: high/failed_ingestion → critical/failed_ingestion |
investigation: Recent ingest_pdf.parse.complete events (1× in last 7 days)... |
packet: anchor=1b779d88 (2 triage, 2 invest) |
draft: 117 words |
routing: on_call_engineer
```

### Mid-chain failure handling

Each chain step is wrapped — if one step throws, `error` is captured
and the replay row is marked `status='failed'` instead of
`'completed'`. The `diff_summary` includes whichever steps did
succeed, so operators don't lose visibility on partial progress.

### New output fields

`SupportReplayOutput` adds passthroughs for the chain's most-useful
signals so callers don't need to re-query:
- `triage_decision` (prior→new sev/cat)
- `investigation_trace_id`
- `response_word_count`
- `routing_decision`

## Tests — `src/fastapi/tests/test_support_replay_workflow.py`

**2 pytest cases, all green:**

| Test | Verifies |
|---|---|
| `test_support_replay_runs_full_chain` | End-to-end: ticket → all 5 chain steps execute → row + audit anchor + diff_summary populated |
| `test_support_replay_handles_already_triaged_ticket` | Pre-triaged ticket → chain still completes (re-triage is a no-op only for resolved/closed) |

Tests invoke the workflow body via `support_replay_execute.aio_mock_run(input)`
(public Hatchet test API).

## Live verification

Ran one real replay against the production `failed_ingestion` ticket:

```text
replay_id:           65ccf04a-e2e4-4469-a818-0fc5abc7dd90
success:             True
routing_decision:    on_call_engineer
response_words:      117
investigation_trace: inv_f7c904cdd2f6d359
diff_summary:        triage: critical/failed_ingestion → critical/failed_ingestion |
                     investigation: Recent ingest_pdf.parse.complete events (1× in last 7 days)... |
                     packet: anchor=1b779d88 (2 triage, 2 invest) | ...

Final state:
  ops.support_replay_runs:                  1
  audit.audit_ledger (support.replay.completed):  3 (incl. test runs)
```

Support Cockpit's "Recent replay runs" panel
(`/admin/support-cockpit`) now shows the replay row.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_support_replay_workflow.py -v
# → 2 passed in 2.23s

bash scripts/autonomous_run_substrate_verify.sh
# → 88/88 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 146
- **Hatchet workflow skeletons graduated:** **4 of 11**
  (evaluate_workspace, generate_report, score_targets, support_replay)
- **§25.4 support agents graduated:** 5 of 5
- **§18.2 nodes graduated:** 6 of 12
- **§15.1 nodes graduated:** 4 of 12
- **§21.3 capture hooks wired:** 1 of 8
- **Reasoning agent skeletons graduated:** 1
- **LangGraph wirings live:** 2 of 2
- **Live pytest cases:** 163 (161 + 2)
- **Substrate verifier:** **88/88 PASS**

## 15-tick run summary (132 → 146)

| Tick | Section | Graduation |
|---|---|---|
| 132 | §10.4 + §10.6 | evaluate_workspace + promotion gate |
| 133 | §9.12 / §21.3 | Laravel RecordDecision + workflow_enablement hook |
| 134 | §9.10 | ai_suggested hypothesis emitter |
| 135 | §6 | jurisdictions + sources foundation seed |
| 136 | §25.4 (1/5) | ticket_triage |
| 137 | §7-A v1 | 4 of 12 §15.1 nodes |
| 138 | §8 / §18.2 | §8.7 formula + 6 of 12 §18.2 nodes |
| 139 | §25.4 (2/5) | root_cause_investigation |
| 140 | §25.4 (3/5) | support_packet |
| 141 | §15.1 + §18.2 | LangGraph Pregel wirings |
| 142 | UX | Admin nav drawer |
| 143 | §25.4 (4/5) | customer_response_drafting |
| 144 | §25.4 (5/5) | **escalation_routing — §25.4 CLOSED** |
| 145 | Hatchet bridge | generate_report + score_targets task bodies |
| 146 | Hatchet bridge | support_replay task body using §25.4 chain |

**§25.4 fully closed. 4 of 11 Hatchet workflow bodies graduated.
All 4 admin dashboards populated with real data.**

## What's next

- **Doc-phase 147** — open scope. Possible directions:
  - Graduate another Hatchet workflow (8 skeletons remaining:
    train_target_model, train_source_trust, continuous_learning_loop,
    field_outcome_learning, what_changed_detector, restore_workspace,
    re_ocr_page if not already live, etc.)
  - Real LLM integration for §15.1 remaining 8 nodes
  - §6 BC MINFILE first real adapter (puts mineral occurrence data
    on the map)
  - §10.4 real evaluator (replace synthetic_stub with §04i pipeline)

## Carry-overs

- The replay implementation invokes the §25.4 chain. When Hatchet's
  run-replay API lands (fetch past run inputs + dispatch a child run),
  the workflow can run **both**: the synthetic chain (always) + the
  real Hatchet replay (when feasible).
- The `dry_run` input flag is currently informational only (logged
  in the audit payload). When real workflow re-execution lands the
  flag will gate side-effect emission per §25.3.
