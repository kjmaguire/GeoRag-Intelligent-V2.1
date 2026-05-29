## Doc-phase 147 handoff — what_changed_detector Hatchet task body graduated

**Status:** Live + 4/4 pytest cases + real signal on Default Workspace. **89/89 substrate verifier**.

## What landed

`what_changed_detector` Hatchet workflow (§9.13 / §21.7) — doc-phase 94
skeleton → doc-phase 147 graduation.

Delta-detection task body that scans the audit ledger + silver tables
for a workspace within a time window and produces structured delta
counts. Feeds the §7.2 `what_changed` report template.

### Pipeline

```text
acquire pool
  ↓
count ingest_pdf.* / ingest.* / ocr.*  audit anchors in window
count public_geoscience.pull.complete  audit anchors in window
count public_geoscience.pull.updated   audit anchors in window
count silver.decision_records          decided_at in window
count silver.hypotheses                created_at in window
count ops.support_tickets              reported_at in window
count audit.audit_ledger               created_at in window (total)
  ↓
emit `workspace.what_changed.detected` audit anchor with payload
  ↓
return WhatChangedOutput with all counts
```

### Extended output fields

Added 4 fields beyond the original spec for observability:
- `new_decision_count` — silver.decision_records in window
- `new_hypothesis_count` — silver.hypotheses in window
- `new_support_ticket_count` — ops.support_tickets in window
- `total_audit_anchors_in_window` — total audit_ledger rows in window

These give operators visibility on workspace activity without needing
to write SQL.

### Real (not synthetic) graduation

This is **fully real** — no synthetic-stub piece. The two fields that
return 0 are honest "table not present yet" / "feature not graduated"
markers:
- `new_claim_count` returns 0 (silver.claim_ledger table doesn't exist yet)
- `target_score_shift_count` returns 0 (awaits §18 scoring delta detection)

## Tests — `src/fastapi/tests/test_what_changed_detector.py`

**4 pytest cases, all green:**

| Test | Verifies |
|---|---|
| `test_what_changed_detector_empty_workspace_returns_zeros` | Fresh workspace → all counts zero |
| `test_what_changed_detector_captures_default_workspace_signal` | Default Workspace 7d → ≥9 hypotheses + ≥6 tickets + >30 total audits |
| `test_what_changed_detector_emits_audit_anchor` | `workspace.what_changed.detected` audit row lands |
| `test_what_changed_detector_narrow_window_returns_lower_counts` | 1-second window ≤ 30-day window |

## Live verification — 7-day delta on Default Workspace

```text
success:                  True
new_ingestion_count:      1
new_public_record:        0
new_decision_count:       0  (decisions are on platform_ops workspace)
new_hypothesis_count:     9
new_support_ticket_count: 6
total_audit_in_window:    170
```

170 audit anchors in the window — composed of:
- 9 hypothesis.generated (doc-phase 134)
- 10 support.ticket.triaged (doc-phase 136)
- 9 support.ticket.investigated (doc-phase 139)
- 8 support.packet.assembled (doc-phase 140 + replay)
- 6 support.ticket.response_drafted (doc-phase 143)
- 7 support.ticket.escalation_routed (doc-phase 144)
- 1 support.replay.completed (doc-phase 146)
- workspace.what_changed.detected (this run + tests)
- 1 ingest_pdf.* (from earlier ingestion activity)
- other system anchors

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_what_changed_detector.py -v
# → 4 passed in 2.45s

bash scripts/autonomous_run_substrate_verify.sh
# → 89/89 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 147
- **Hatchet workflow skeletons graduated:** **5 of 11**
  (evaluate_workspace, generate_report, score_targets, support_replay,
  what_changed_detector)
- **§25.4 support agents graduated:** 5 of 5 (closed)
- **§18.2 nodes graduated:** 6 of 12
- **§15.1 nodes graduated:** 4 of 12
- **§21.3 capture hooks wired:** 1 of 8
- **Reasoning agent skeletons graduated:** 1
- **LangGraph wirings live:** 2 of 2
- **Live pytest cases:** 167 (163 + 4)
- **Substrate verifier:** **89/89 PASS**

## Remaining Hatchet workflow skeletons (6 of 11)

| Workflow | Notes |
|---|---|
| `train_target_model` | Needs target_outcomes data (long-horizon) |
| `train_source_trust` | Needs trust ground-truth labels |
| `continuous_learning_loop` | Feedback loop scheduler; data-dependent |
| `field_outcome_learning` | Drill outcome ingestion; needs SME input |
| `restore_workspace` | DR / backup restore; needs backup infra |
| `re_ocr_page` | Already partially live? Worth checking |

## What's next

- **Doc-phase 148** — check `re_ocr_page` skeleton status; or graduate
  `restore_workspace` Hatchet task body (uses silver.* + audit chain)
- **Doc-phase 149** — §6 BC MINFILE first real adapter (puts mineral
  occurrence rows in `public_geoscience.pg_mineral_occurrence`)
- **Doc-phase 150** — wire `what_changed_detector` into the §7.2
  `what_changed` report template so the report shows the deltas
- **Doc-phase 151+** — open scope

## Carry-overs

- `silver.claim_ledger` table doesn't exist yet — the
  `new_claim_count` field returns 0 with that justification. When
  the claim-ledger schema lands, swap the synthetic zero for a real
  count.
- The delta numbers are workspace-scoped. Cross-workspace deltas
  (e.g. "what changed across all customers' workspaces this week")
  would need a separate "global" entry point — not in scope today
  but a clear extension.
