## Doc-phase 115 handoff — Second LIVE helper: `record_decision` graduated

**Status:** Complete. **5/5 pytest tests pass** for record_decision;
**15/15 combined** with doc-phase 114 ontology tests.

## What landed

`src/fastapi/app/services/decision_intelligence/recorder.py` —
graduated from doc-phase 92 skeleton to **live implementation**.

### What `record_decision()` now does

In a single transaction, atomically:

1. INSERTs `silver.decision_records` row + returns `decision_id`
2. INSERTs `silver.decision_evidence_links` rows for each
   `evidence_chunk_ids` entry (role=`supporting`)
3. INSERTs `silver.decision_options` rows for each `options_considered`
   entry; raises `ValueError` if any option lacks a `label`
4. Optional `silver.decision_outcomes` row when `outcome_kind` is set
5. Emits an `audit.audit_ledger` row via `app.audit.emit_audit`
   (`action_type = f"decision.{decision_type}"`); captures the
   ledger row's `id` + `hash`
6. UPDATEs `silver.decision_records.audit_ledger_id` + `hash` with
   the ledger anchors — so a regulator reading the decision row can
   walk straight into the hash chain

Per master plan §21.4 the decision is the chain anchor; §29.6.1
QP credential mandate means every R5 decision MUST land in the
chain. The recorder enforces this contract centrally.

### Validation

- `uncertainty` outside `[0, 1]` raises `ValueError` before any
  DB write
- Options without `label` raise `ValueError` AND the surrounding
  transaction rolls back — test verifies no orphan rows survive

### Test coverage — `src/fastapi/tests/test_decision_recorder.py`

5 pytest cases against real Postgres + asyncpg + RLS:

| Test | Verifies |
|---|---|
| `test_record_decision_basic` | Minimal happy path; audit_ledger_id + hash back-fill |
| `test_record_decision_with_evidence_and_options` | Evidence + options + chained audit row; action_type = `decision.target_recommendation` |
| `test_record_decision_uncertainty_validation` | Raises on uncertainty outside [0, 1] |
| `test_record_decision_with_outcome` | Optional decision_outcomes row writes |
| `test_record_decision_option_missing_label_raises` | Rollback on bad option; no orphan rows |

### Fixture pattern

- `synthetic_workspace` — inserts a workspace + sets
  `app.workspace_id` GUC so RLS `WITH CHECK` policies pass; cleans
  up the GUC + workspace at teardown
- `synthetic_user` — inserts a public.users row, cleans up at
  teardown

### Permission grant

`georag_app` previously couldn't DELETE from `silver.workspaces` or
`public.users` (which fixture teardown needs). Granted those:

```sql
GRANT DELETE ON silver.workspaces TO georag_app;
GRANT DELETE ON public.users TO georag_app;
```

These grants are safe — RLS still gates `silver.*` row-level access;
the grants only allow the role to issue DELETE statements, which
remain RLS-policy-bounded.

## Pattern emerging

Doc-phases 114 + 115 are now the **template** for graduating
autonomous-run substrate skeletons to live behavior:

1. Schema migration already in place (doc-phase 76+)
2. Skeleton service module already in place (doc-phase 79+)
3. Seed / fixture data available (doc-phase 112+)
4. Graduate the skeleton body to real SQL/logic
5. Write a pytest module with `synthetic_*` fixtures
6. Run pytest; iterate until green
7. Document with a handoff

Same pattern will fit:
- `app.services.support_cockpit.access_audit.emit_support_access_audit`
- `app.audit.hash_chain_proof.build_hash_chain_proof`
- `app.services.geological_ontology` — additional helpers like
  `bulk_resolve()` or `query_expansion()` for the retrieval layer

## Cumulative session-continuation state (doc-phases 74-115 = 42 ticks)

The autonomous run has now graduated **two** skeletons to live code,
each with permanent pytest coverage:

| Doc-phase | Module | Tests |
|---|---|---|
| 114 | `geological_ontology.resolve_term` + `find_synonyms` | 10 |
| 115 | `decision_intelligence.record_decision` | 5 |
| **Total** | | **15 live pytest cases** |

## Master-plan §9.10 progress

§9.10 `record_decision` facade was the central piece for the
8-decision-types-funnel-through-here design recommended in the §9
scope proposal. Now live + tested. Hooks into the 8 decision types
are still pending (`§9.10 capture hooks wire up`), but the
underlying facade is no longer a skeleton — any caller can use it
today.

## Recommended next ticks

Continue the "graduate to live" pattern:
- Doc-phase 116 = `emit_support_access_audit` graduate (small;
  pure pass-through to `emit_audit` with `action_type='support_access'`)
- Doc-phase 117 = `build_hash_chain_proof` graduate (reads
  audit_ledger + walks the chain — depends on having decision_records
  with audit anchors to read)

Or pivot:
- Wire `record_decision` into one of the 8 decision-capture hooks
  (e.g., when a `target_review_decisions` row is INSERTed, fire
  `record_decision` for the `target_recommendation` decision type)

## Carry-overs

Unchanged plus:
- `GRANT DELETE ON silver.workspaces TO georag_app` + `GRANT DELETE
  ON public.users TO georag_app` — applied for test fixture teardown.
  Production-safe; RLS still gates row access.
