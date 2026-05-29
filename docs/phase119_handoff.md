## Doc-phase 119 handoff — Sixth LIVE helper + first downstream consumer

**Status:** Complete. 6 new pytest cases; **36 live pytest cases
total**; verifier **66/66**.

## What landed

### `get_workspace_decision_summary` — first downstream consumer

`src/fastapi/app/services/decision_intelligence/summary.py` — new
live aggregation helper that reads `silver.decision_records` and
returns per-workspace decision telemetry. **First reader-side
function in this autonomous run that depends on the writer-side
`record_decision` (doc-phase 115).** Demonstrates the substrate
growing real downstream query surface.

### Output dataclasses

```python
@dataclass
class DecisionTypeCounts:
    decision_type: str
    total: int
    accepted: int
    modified: int
    rejected: int
    signed_off: int
    other: int

@dataclass
class WorkspaceDecisionSummary:
    workspace_id: UUID
    window_start: datetime
    window_end: datetime
    total_decisions: int
    decisions_with_audit_anchor: int   # how many have an audit_ledger_id
    by_type: list[DecisionTypeCounts]
    mean_uncertainty: float | None     # ignores NULLs
    latest_decision_at: datetime | None
```

### `ALL_DECISION_TYPES` constant

Mirror of the §21.3 CHECK enum so callers can iterate type names
without hard-coding strings. 8-element tuple.

### Pytest — `tests/test_decision_summary.py` (6 cases)

| Test | Verifies |
|---|---|
| `test_all_decision_types_constant_matches_check_enum` | Tuple has 8 types; matches §21.3 vocabulary |
| `test_empty_workspace_summary` | Zero totals + None mean_uncertainty for fresh workspace |
| `test_summary_after_three_decisions` | Mixed types (schema_mapping × 2 + export_approval × 1) → correct buckets + 100% audit-anchor coverage |
| `test_window_filter_excludes_out_of_range` | Past window returns 0; near-now window finds the decision |
| `test_invalid_window_raises` | end ≤ start raises ValueError |
| `test_mean_uncertainty_ignores_nulls` | AVG over NULL-tolerant uncertainty matches Postgres semantics |

### Verifier extension — 6 live pytest gates

| Gate | Tests |
|---|---|
| `pytest:ontology_resolver` | 10 |
| `pytest:decision_recorder` | 5 |
| `pytest:support_access_audit` | 4 |
| `pytest:hash_chain_proof` | 5 |
| `pytest:langfuse_link` | 6 |
| **`pytest:decision_summary`** | **6 (new)** |
| **Total live pytest cases** | **36** |

```
bash scripts/autonomous_run_substrate_verify.sh
# → 66/66 checks passed
```

## Pattern progression — 6 live helpers + first downstream consumer

| Doc-phase | Module | Role | Pytest |
|---|---|---|---|
| 114 | `geological_ontology.resolve_term` + `find_synonyms` | reader of seeded data | 10 |
| 115 | `decision_intelligence.record_decision` | writer | 5 |
| 116 | `support_cockpit.emit_support_access_audit` | writer (composes emit_audit) | 4 |
| 117 | `audit.hash_chain_proof.build_hash_chain_proof` | reader of audit ledger | 5 |
| 118 | `support_cockpit.open_trace_with_audit` | composer (URL + audit emitter) | 6 |
| **119** | **`decision_intelligence.get_workspace_decision_summary`** | **reader of decision_records** | **6** |

The substrate now has writers + readers + composers + downstream
consumers. The §21 decision intelligence + §22 audit ledger +
§9.2 ontology layers all have at least one live function each.

## Cumulative session-continuation state (doc-phases 74-119 = 46 ticks)

- 46 doc-phase ticks
- 7 scope-proposal docs (§6/§7/§8/§9/§10/§11/§12)
- **6 live helpers** graduated from skeleton
- **36 permanent pytest cases** protecting the substrate
- Substrate verifier **66/66 PASS**
- 14 Eloquent models + 5 factories
- 26 new database tables across 4 new schemas
- 10 Hatchet workflows registered in AI pool
- 83 ontology terms + 134 synonyms seeded
- §15.3 hash-chain proof loop end-to-end functional

## Recommended next ticks

Continued downstream-consumer pattern:
- `get_workspace_ontology_stats` — aggregates
  `silver.geological_ontology_terms` per class; feeds an admin
  dashboard surfacing SME-population progress
- `get_workspace_audit_excerpt` — paginates audit_ledger rows for
  the customer-visible audit history (per §25.3)
- `get_support_ticket_history` — aggregates `ops.support_tickets`
  + traces for cockpit UI

Or pivot:
- Decision-capture hook integration: when a Laravel sign-off action
  fires, dispatch a queued job that calls `record_decision` —
  cross-stack integration test

## Carry-overs

Unchanged. The substrate is genuinely self-reinforcing now —
graduated writers feed downstream readers, all with permanent
pytest coverage.
