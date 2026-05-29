## Doc-phase 120 handoff — Seventh LIVE helper: `get_ontology_class_stats`

**Status:** Complete. 8 new pytest cases; **44 live pytest cases
total**; verifier **67/67**.

## What landed

### `get_ontology_class_stats` — second downstream consumer

`src/fastapi/app/services/geological_ontology/stats.py` — aggregates
`silver.geological_ontology_terms` + synonyms across all 12 §20.1
classes. Mirror of the doc-phase 119 `get_workspace_decision_summary`
pattern but on the ontology side (no workspace scope; ontology is
global).

### Output dataclasses

```python
@dataclass
class OntologyClassStats:
    ontology_class: str
    term_count: int
    synonym_count: int
    most_recent_term_at: datetime | None
    status: 'empty' | 'mechanical_seeded' | 'sme_populating' | 'populated'
    populated_threshold: int

@dataclass
class OntologyStatsSummary:
    by_class: list[OntologyClassStats]
    total_terms: int
    total_synonyms: int
    classes_populated: int       # how many meet threshold
    classes_with_any_data: int   # how many have ≥ 1 term
    sme_pass_complete: bool      # all 12 ≥ threshold
```

### Per-class populated thresholds (§9 scope estimates)

| Class | Floor | Notes |
|---|---|---|
| commodity | 30 | Periodic-table + critical-minerals |
| geological_age | 20 | Eons + eras + periods + epochs |
| resource_class | 6 | CIM categories |
| deposit_model | 8 | 10 launch types target |
| lithology | 150 | 200-300 BGS taxonomy |
| alteration | 25 | 30-50 types |
| structure | 15 | 20-30 types |
| mineral_assemblage | 20 | Dozens in practice |
| host_rock | 20 | Cross-references |
| tectonic_setting | 12 | 15-25 entries |
| geochemistry | 10 | Pathfinder elements + ratios |
| geophysics | 10 | Signature patterns |

### Status heuristic

- `empty` — 0 terms
- `mechanical_seeded` — ≥ 1 term in a mechanical class
  (commodity / geological_age / resource_class) but below threshold
- `sme_populating` — ≥ 1 term in an SME-pending class but below
  threshold
- `populated` — term_count ≥ class threshold

### Today's state (per the live test outputs)

- `commodity` = 47 terms → **populated**
- `geological_age` = 29 terms → **populated**
- `resource_class` = 7 terms → **populated**
- 9 SME-pending classes = 0 terms → **empty**
- `sme_pass_complete = False` (3 of 12 populated)

When the §9.3 SME pass fills the remaining 9 classes above their
thresholds, `sme_pass_complete` flips to True automatically.

### Pytest — `tests/test_ontology_stats.py` (8 cases)

| Test | Verifies |
|---|---|
| `test_stats_includes_all_12_classes` | Default call returns all 12 §20.1 classes |
| `test_commodity_class_populated` | 47 ≥ 30 → 'populated' |
| `test_geological_age_populated` | 29 ≥ 20 → 'populated' |
| `test_resource_class_populated` | 7 ≥ 6 → 'populated' |
| `test_sme_pending_classes_show_empty` | 9 SME classes have valid status |
| `test_summary_rollup_counts` | total ≥ 83 terms, ≥ 134 synonyms |
| `test_sme_pass_complete_flag` | True iff classes_populated == 12 |
| `test_per_class_stats_dataclass` | Field types + status enum validity |

### Verifier extension — 7 live pytest gates

| Gate | Tests |
|---|---|
| `pytest:ontology_resolver` | 10 |
| `pytest:decision_recorder` | 5 |
| `pytest:support_access_audit` | 4 |
| `pytest:hash_chain_proof` | 5 |
| `pytest:langfuse_link` | 6 |
| `pytest:decision_summary` | 6 |
| **`pytest:ontology_stats`** | **8 (new)** |
| **Total live pytest cases** | **44** |

```
bash scripts/autonomous_run_substrate_verify.sh
# → 67/67 checks passed
```

## Pattern progression — 7 live helpers

| Doc-phase | Module | Role | Pytest |
|---|---|---|---|
| 114 | `resolve_term` + `find_synonyms` | reader | 10 |
| 115 | `record_decision` | writer | 5 |
| 116 | `emit_support_access_audit` | writer | 4 |
| 117 | `build_hash_chain_proof` | reader | 5 |
| 118 | `open_trace_with_audit` | composer | 6 |
| 119 | `get_workspace_decision_summary` | aggregator (decision side) | 6 |
| **120** | **`get_ontology_class_stats`** | **aggregator (ontology side)** | **8** |

## Cumulative session-continuation state (doc-phases 74-120 = 47 ticks)

- 47 doc-phase ticks
- 7 scope-proposal docs (§6/§7/§8/§9/§10/§11/§12)
- **7 live helpers** graduated from skeleton
- **44 permanent pytest cases** protecting the substrate
- Substrate verifier **67/67 PASS**
- 26 new database tables across 4 new schemas
- 10 Hatchet workflows registered
- 14 Eloquent models + 5 factories
- 83 ontology terms + 134 synonyms seeded
- §15.3 hash-chain proof loop end-to-end functional

## Recommended next ticks

Doc-phase 121 onward:
- More aggregators: `get_workspace_audit_excerpt`,
  `get_support_ticket_history`, `get_workspace_hypothesis_count`
- Cross-stack decision-capture hooks — when Laravel sign-off fires,
  dispatch a job that calls `record_decision`
- Frontend stubs for the §10.7 Eval Dashboard backed by the live
  `get_workspace_decision_summary` + `get_ontology_class_stats`

Or stop the autonomous run — the substrate has reached a strong
self-reinforcing terminal state.

## Carry-overs

Unchanged. The substrate now has 7 live helpers, including 2
aggregator-style queries on top of the writer-side primitives.
Future graduations + downstream consumers follow the same template.
