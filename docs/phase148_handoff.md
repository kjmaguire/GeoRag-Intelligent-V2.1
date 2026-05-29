## Doc-phase 148 handoff — restore_workspace Hatchet task body (dry-run path)

**Status:** Live + 4/4 pytest cases + 90/90 substrate verifier.

## What landed

`restore_workspace` Hatchet workflow (§11.3 / §26.3) — doc-phase 100
skeleton → doc-phase 148 graduation of the **dry-run consistency-check
path**.

Real cross-store restore (Postgres + Neo4j + Qdrant + Redis +
SeaweedFS) needs backup infrastructure that doesn't exist yet (§11.1).
The dry-run path is fully real today and gives operators a baseline
consistency-check tool they can run against any workspace.

### Two paths

| `dry_run` | Behavior |
|---|---|
| `True` (default) | **Live**: verifies workspace exists, counts per-table state, emits `workspace_restore` audit anchor with structured payload |
| `False` | **Explicit failure**: returns success=false + failure_stage='precheck' + clear message pointing at the §11.1 backup-infrastructure dependency. Never silently destructive. |

### Dry-run pipeline

1. Verify `workspace_id` exists in `silver.workspaces`
2. Count rows in 5 baseline tables, scoped to the workspace:
   - silver.workspaces, silver.hypotheses, silver.decision_records,
     audit.audit_ledger, ops.support_tickets
3. Compose `consistency_check_results` dict (workspace name/slug,
   manifest_uri, row_counts, stores_in_baseline)
4. Emit `workspace_restore` audit anchor with full structured payload
5. Return `RestoreWorkspaceOutput`

### Real-mode guard

When `dry_run=False`, returns immediately with `failure_stage='precheck'`
and a message identifying the missing backup infrastructure. This
guard prevents operator typos from accidentally triggering a
destructive op against the live workspace.

## Tests — `src/fastapi/tests/test_restore_workspace.py`

**4 pytest cases, all green:**

| Test | Verifies |
|---|---|
| `test_restore_workspace_dry_run_against_default_workspace` | Default Workspace dry-run returns expected counts (≥9 hypotheses, ≥6 tickets, >100 audits) |
| `test_restore_workspace_unknown_workspace_returns_failure` | Bad UUID → success=false, failure_stage='workspace_lookup' |
| `test_restore_workspace_real_mode_is_explicitly_gated` | `dry_run=False` → success=false, failure_stage='precheck', clear message |
| `test_restore_workspace_emits_audit_anchor` | `workspace_restore` row lands in audit.audit_ledger |

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_restore_workspace.py -v
# → 4 passed in 2.26s

bash scripts/autonomous_run_substrate_verify.sh
# → 90/90 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 148
- **Hatchet workflow skeletons graduated:** **6 of 11**
  (evaluate_workspace, generate_report, score_targets, support_replay,
  what_changed_detector, restore_workspace)
- **§25.4 support agents graduated:** 5 of 5 (closed)
- **§18.2 nodes graduated:** 6 of 12
- **§15.1 nodes graduated:** 4 of 12
- **§21.3 capture hooks wired:** 1 of 8
- **Reasoning agent skeletons graduated:** 1
- **LangGraph wirings live:** 2 of 2
- **Live pytest cases:** 171 (167 + 4)
- **Substrate verifier:** **90/90 PASS**

## 17-tick run summary (132 → 148)

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
| 144 | §25.4 (5/5) | escalation_routing — §25.4 CLOSED |
| 145 | Hatchet bridge | generate_report + score_targets task bodies |
| 146 | Hatchet bridge | support_replay task body |
| 147 | §9.13 | what_changed_detector task body |
| 148 | §11.3 | restore_workspace task body (dry-run) |

## Remaining Hatchet workflow skeletons (5 of 11)

| Workflow | Status |
|---|---|
| `train_target_model` | Skeleton — needs target_outcomes data |
| `train_source_trust` | Skeleton — needs trust ground-truth labels |
| `continuous_learning_loop` | Skeleton — feedback loop scheduler |
| `field_outcome_learning` | Skeleton — drill outcome ingestion |
| `re_ocr_page` | Status unknown — likely already live |

The remaining 4 (excluding re_ocr_page) all need data we don't have
yet. They'd graduate with the same dry-run + audit-anchor pattern
established in doc-phase 146/148.

## What's next

- **Doc-phase 149** — §6 BC MINFILE adapter (real data on the map)
- **Doc-phase 150** — wire `what_changed_detector` output into the §7.2
  what_changed report template
- **Doc-phase 151** — §21.3 capture hooks at additional sites
  (target_recommendation in score_targets workflow, etc.)
- **Doc-phase 152+** — open scope

## Carry-overs

- Add `re_ocr_page` to the substrate verifier explicitly — its status
  is unclear right now. May already be live; worth confirming.
- The dry-run consistency check is per-workspace. A cross-workspace
  "verify all workspaces" wrapper would be useful for operational
  drills.
- When backup infrastructure ships (§11.1), the `dry_run=False`
  path slots in cleanly — the audit anchor + idempotency-key handling
  + structured output are all already wired.
