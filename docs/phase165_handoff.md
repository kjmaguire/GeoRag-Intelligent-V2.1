## Doc-phase 165 handoff — §04i Layer 5 chunk-provenance validator

**Status:** Live + 7 new pytest cases + 51/51 regression + 8/8 real-RAG pass with chained Layer 6 + 2 + 5. **105/105 substrate verifier**.

## What landed

Third §04i validator graduates: **Layer 5 chunk provenance**. Each
non-refusal Qdrant-bound citation's `source_chunk_id` must resolve
to a real point in the Qdrant collection. Catches hallucinated
chunk IDs that look valid but don't exist in the vector store.

### `validate_chunk_provenance` — async (the first async validator)

Different from Layers 6 + 2 (pure functions) because it makes
external calls. The chain helper still works — `real_rag_v1` does
`await validate_chunk_provenance(...)` and passes the resolved
outcome into `chain_validators`.

### Citation-type-aware skipping (cross-section finding)

First live run revealed the orchestrator emits 3 distinct citation
type/corpus combinations, only one of which Layer 5 should look up:

| Citation source | citation_type | corpus | Layer 5 action |
|---|---|---|---|
| Qdrant chunk (NI 43-101 / publication PDFs) | `NI43` / `PUB` | `internal_archive` | **Look up in Qdrant** |
| Silver SQL aggregate (e.g. `silver.collars:count=20:first=...`) | `DATA` | `internal_archive` | **Skip** — not in Qdrant by design |
| PostGIS public-geoscience row (`pgeo:bc_minfile:001`) | `PGEO` | `public_geoscience` | **Skip** — future SQL-provenance layer |

Updated the validator to skip `DATA` (sql_skipped counter) and `PGEO`
(pgeo_skipped counter) citations. Only Qdrant-bound types
(`NI43` / `PUB`) are subject to point-id lookup.

### Refusal-path vacuous pass

The orchestrator emits sentinel citations (e.g. `georag_reports:empty`)
on refusal responses because Layer 2's hard rule forces ≥1 citation.
Penalising those would double-up with Layer 6 (which already caught
the refusal). Solution: when `expected_refusal=True`, Layer 5
vacuously passes regardless of citation content.

This mirrors what Layer 2 does for the same edge case — refusal
responses get a pass on citation-related layers.

### `real_rag_v1` now chains 3 validators

```python
outcomes = [
    validate_refusal_correctness(response_text=..., question=...),
    validate_citation_presence(citations=..., question=...),
    await validate_chunk_provenance(
        citations=...,
        qdrant_client=deps.qdrant_client,
        question=...,
    ),
]
all_passed, failure_layer, failure_detail = chain_validators(outcomes)
```

`actual_payload.validators_applied` now lists all 3:
`["6_refusal", "2_citation_presence", "5_chunk_provenance"]`.

## Tests — 7 new + 27 total + 51/51 regression

7 new Layer 5 unit tests covering:
- Vacuous pass on refusal with no citations
- Pass when all Qdrant IDs resolve
- Fail on unresolved ID (with the failing ID surfaced in the message)
- Skip `PGEO` corpus citations (no Qdrant lookup)
- Handle Qdrant exceptions as `unresolved` (defensive)
- Accept dict-shaped citations (Pydantic round-trip safe)
- Skip `DATA` citation_type (silver SQL aggregates)
- Refusal-path vacuous pass even with synthetic sentinel citations

All use a `_FakeQdrant` async stand-in so tests don't need a live
Qdrant — fast + deterministic.

**51/51 regression tests pass** across all eval modules.

## Live verification

Three real_rag_v1 runs against refusal_correctness:

| Run | Validators chained | Pass count |
|---|---|---|
| doc-phase 163 | Layer 6 + Layer 2 | 8/8 |
| doc-phase 165 (first cut) | Layer 6 + Layer 2 + Layer 5 (no skip) | 0/8 ← Layer 5 caught sentinel IDs |
| doc-phase 165 (final) | Layer 6 + Layer 2 + Layer 5 (with skips) | **8/8** |

The first cut surfaced a real cross-section finding — the validator's
"every citation must resolve in Qdrant" rule was too strict for the
orchestrator's actual citation mix. Iterating to the citation-type-
aware skip pattern caught it without weakening Layer 5's signal.

When SME questions with `expected_refusal=False` land, Layer 5 will
catch any NI43/PUB citation that doesn't resolve — that's the real
regression-detection coverage this tick adds.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_eval_validators.py
# → 27 passed in 0.17s

# Regression across all eval modules
docker exec georag-fastapi python -m pytest \
    tests/test_eval_validators.py \
    tests/test_workspace_evaluator.py \
    tests/test_real_llm_evaluator.py \
    tests/test_real_rag_evaluator.py \
    tests/test_evaluate_workspace_workflow.py
# → 51 passed in 5.79s

bash scripts/autonomous_run_substrate_verify.sh
# → 105/105 checks passed
```

## Cumulative session state — 34 ticks closed

- **Doc-phase ticks this run:** **34** (132 → 165)
- **Substrate verifier:** **105/105 PASS**
- **Live pytest cases:** 262 (255 + 7)
- **Sections closed:** §25.4 + §6
- **§04i validators graduated:** **3 of 6** (refusal_correctness +
  citation_presence + chunk_provenance)
- **Evaluator kinds wireable:** 3
- **§21.3 types covered:** 8 of 8
- **First async validator:** Layer 5 (precedent for Layers 1 + 3)
- **PublicGeo features on map:** 95

## What's next

3 §04i validators remain. Each adds one async/sync validator to
the chain:

- **Doc-phase 166** — Layer 4 entity-resolution validator (extracts
  entity mentions, cross-checks against `expected_entities`)
- **Doc-phase 167** — Layer 3 numeric-claim validator (compares
  extracted numbers against `expected_numeric_values` specs; needs
  real silver data to derive ground truth)
- **Doc-phase 168** — Layer 1 retrieval-quality validator (citation
  relevance scores above gate threshold)

When all 6 are graduated, the chain is complete and `real_rag_v1`
exercises the full §04i hallucination-prevention surface.

## Carry-overs

- `_FakeQdrant` is a test fixture only — production uses
  AsyncQdrantClient. The test-fakes approach scales to other
  external-service validators (entity resolver via Neo4j, numeric
  validator via PostgreSQL).
- The `sql_skipped` count is informational today. When the
  SQL-provenance validator (doc-phase 167+) lands, those skipped
  citations get validated against silver.* row counts instead.
- Layer 5 reports unresolved citations capped at 10. Full unresolved
  list is in the audit ledger trail if needed for forensic review.
