## Doc-phase 117 handoff — Fourth LIVE helper: `build_hash_chain_proof`

**Status:** Complete. 5 pytest cases pass; **24 live pytest cases
total**; verifier **64/64**.

## What landed

### `build_hash_chain_proof` graduated to live

`src/fastapi/app/audit/hash_chain_proof.py` — now reads
`audit.audit_ledger` over a workspace + time window, calls the
existing `audit.recompute_hash(...)` PL/pgSQL function for each row,
and emits a structured JSON proof.

Single source of truth: the recipe lives in the Postgres function
(per the §22 audit_ledger_hash_recipe.md). The Python layer calls
into it rather than re-implementing the hash logic — the same code
path the nightly verifier uses.

### Output JSON shape (verified)

```json
{
  "report_id": "...",
  "workspace_id": "...",
  "recipe_version": "v1",
  "verification_range": {"start": "...", "end": "..."},
  "rows": [
    {
      "id": "...",
      "created_at": "2026-05-13T...",
      "action_type": "decision.schema_mapping",
      "actor_kind": "user",
      "actor_id": 42,
      "target_schema": "silver",
      "target_table": "decision_records",
      "target_id": "...",
      "payload_text": "...",       // raw jsonb::text for external auditors
      "payload": {...},             // parsed for convenience
      "previous_hash_hex": "...",
      "stored_hash_hex": "...",
      "recomputed_hash_hex": "...",
      "match": true
    }
  ],
  "summary": {
    "row_count": 3,
    "all_match": true,
    "broken_ids": []
  }
}
```

### Optional `report_id` filter

When supplied, narrows the proof to audit rows whose
`payload->>'report_id'` OR `target_id` equals the report id. Useful
for the §15.3 per-report bundle.

### Pytest — `tests/test_hash_chain_proof.py` (5 cases)

| Test | Verifies |
|---|---|
| `test_empty_window_returns_empty_proof` | Empty window → row_count=0, all_match=True |
| `test_invalid_window_raises` | end ≤ start raises ValueError |
| `test_proof_captures_single_decision` | record_decision → audit row → proof shows match=True |
| `test_proof_captures_multiple_decisions_in_order` | **Chain linkage** — each row's previous_hash_hex == prior row's stored_hash_hex |
| `test_proof_report_id_filter` | report_id filter narrows to anchor decision only |

The chain-linkage test is the regulatory-anchor verification — it
proves the §22 recipe actually chains rows in order. Independent
auditors can re-walk the same chain from the JSON without GeoRAG
code.

### Verifier extension — 4 live pytest gates

`scripts/autonomous_run_substrate_verify.sh` now runs 4 pytest
modules end-to-end:

| Gate | Tests |
|---|---|
| `pytest:ontology_resolver` | 10 |
| `pytest:decision_recorder` | 5 |
| `pytest:support_access_audit` | 4 |
| **`pytest:hash_chain_proof`** | **5 (new)** |
| **Total live pytest cases** | **24** |

```
bash scripts/autonomous_run_substrate_verify.sh
# → 64/64 checks passed
```

## Pattern progression — 4 of N skeletons graduated

| Doc-phase | Module | Pytest cases |
|---|---|---|
| 114 | `geological_ontology.resolve_term` + `find_synonyms` | 10 |
| 115 | `decision_intelligence.record_decision` | 5 |
| 116 | `support_cockpit.emit_support_access_audit` | 4 |
| **117** | **`audit.hash_chain_proof.build_hash_chain_proof`** | **5** |

24 live pytest cases gating the substrate. Each module follows the
same graduation pattern + permanent pytest coverage.

## Why this matters — closes the §15.3 regulatory loop

§15.3 promised every report bundle ships with a
`hash_chain_proof.json`. The piece chain was:

1. **emit_audit** (Phase 0) — writes audit rows with chain hashes
2. **record_decision** (doc-phase 115) — fires emit_audit for §21
   decisions
3. **emit_support_access_audit** (doc-phase 116) — fires emit_audit
   for §25.3 ops access
4. **build_hash_chain_proof** (this tick) — reads + verifies the
   chain

Now an external auditor can take any `hash_chain_proof.json`, walk
it row-by-row applying the §22 recipe, and verify the chain without
GeoRAG code. Zero-trust + tamper-evident, as promised.

## Cumulative session state (doc-phases 74-117 = 44 ticks)

44 doc-phases through this autonomous run continuation. 4 live
helpers + 24 pytest cases. Verifier 64/64. The substrate is now
genuinely battle-tested against real database I/O.

## Recommended next ticks

Doc-phase 118 = graduate `open_trace_with_audit` (§10.13). Trivial
— combines existing `build_langfuse_trace_url` (already pure +
working) with new `emit_support_access_audit`. ~3 lines of body.

Doc-phase 119 = bigger lift — wire `record_decision` into one of
the 8 capture hooks (e.g., when `targeting.target_review_decisions`
INSERT fires, call `record_decision('target_recommendation', ...)`
via a database trigger or a service-layer hook). That's where the
decision intelligence layer becomes self-sustaining.

## Carry-overs

Unchanged. `app.workspace_id` GUC behavior on connection pool
shared across tests works correctly because each test sets it
before use.
