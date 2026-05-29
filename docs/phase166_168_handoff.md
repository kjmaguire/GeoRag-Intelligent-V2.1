## Doc-phases 166-168 handoff — §04i Layer 4 + Layer 3 + Layer 1 — FULL 6-LAYER CHAIN COMPLETE

**Status:** All 6 §04i validators live + 18 new pytest cases + 69/69 regression + 8/8 real-RAG pass with full chain. **105/105 substrate verifier**.

**Third major section milestone in this run: §04i hallucination-prevention surface is now 6 of 6 validators graduated.**

## What landed across 3 ticks

### Doc-phase 166 — Layer 4 entity resolution

`validate_entity_resolution(response_text, question)` — synchronous.
Scans response text for entity names extracted from
`question.expected_entities`. Case-insensitive substring matching.

Name extraction supports SME-style entries with any of:
- `name` (primary)
- `entity_name` (alias)
- `expected_value` (string variant)

Vacuous-pass cases:
- `expected_refusal=True` → refusal exempts entity checks
- `_extract_entity_names()` returns no names (structural-only specs
  like `{"expected_route": "accept"}` or `{"required_section_ids": [...]}`)

### Doc-phase 167 — Layer 3 numeric-claim validation

`validate_numeric_claims(response_text, question)` — synchronous.
For each entry with a concrete `expected_value`, scans response text
for any number within `tolerance_pct` of it. Pass iff every
expected_value finds a match.

Vacuous-pass cases:
- `expected_refusal=True`
- `expected_numeric_values` is empty
- All entries lack `expected_value` (silver-data ground truth needed)

When silver data ingestion lands + per-question SQL ground-truth
derivation is wired in a future graduation, Layer 3 lights up against
the existing `path`/`source_table`/`tolerance_pct` specs in the seeded
numeric_grounding question set.

### Doc-phase 168 — Layer 1 retrieval quality

`validate_retrieval_quality(citations, question, min_relevance_score=0.5)`
— synchronous. Each citation with a `relevance_score` is checked
against the gate threshold. Citations lacking the field are skipped
(unscored_count); Layer 1 only gates what was scored.

Default threshold (0.5) matches the existing chat-side cross-encoder
gate. Future tickets can tune per-question-set thresholds via the
§10.6 promotion gate.

### `real_rag_v1` — full 6-layer chain

```python
outcomes = [
    validate_refusal_correctness(...),       # Layer 6 (doc-phase 159)
    validate_citation_presence(...),         # Layer 2 (doc-phase 163)
    await validate_chunk_provenance(...),    # Layer 5 (doc-phase 165)
    validate_entity_resolution(...),         # Layer 4 (doc-phase 166)
    validate_numeric_claims(...),            # Layer 3 (doc-phase 167)
    validate_retrieval_quality(...),         # Layer 1 (doc-phase 168)
]
all_passed, failure_layer, failure_detail = chain_validators(outcomes)
```

`actual_payload.validators_applied` lists all 6 layers in the order
they run. The `failure_layer` namespace is bucketed
(`'6_refusal'`, `'2_citation_presence'`, `'5_chunk_provenance'`,
`'4_entity_resolution'`, `'3_numeric_claims'`, `'1_retrieval_quality'`)
so downstream consumers (promotion gate, dashboard) can route per-layer.

## Tests — 18 new + 45 total in validators + 69/69 regression

`src/fastapi/tests/test_eval_validators.py` — **45 pytest cases** total:

Layer 6 refusal: 9 cases (doc-phase 159 / 163)
Layer 2 citation_presence: 6 cases (doc-phase 163)
Layer 5 chunk_provenance: 9 cases (doc-phase 165, async)
Layer 4 entity_resolution: **6 new** (doc-phase 166)
Layer 3 numeric_claims: **7 new** (doc-phase 167)
Layer 1 retrieval_quality: **5 new** (doc-phase 168)
chain_validators: 4 cases

Helper `_make_question()` extended to accept `expected_entities` +
`expected_numeric_values` kwargs.

**69/69 regression tests pass** across all eval modules.

## Live verification — 8/8 with full 6-layer chain

```text
Validators chained in order:
  6_refusal             → all refusals correctly detected
  2_citation_presence   → vacuous-pass on refusal path
  5_chunk_provenance    → vacuous-pass on refusal path
  4_entity_resolution   → vacuous-pass on refusal path
  3_numeric_claims      → vacuous-pass on refusal path
  1_retrieval_quality   → vacuous-pass on refusal path

Result: 8/8 pass
```

The refusal_correctness questions all pass because Layer 6 catches
the refusal + Layers 2/5/4/3/1 all vacuously pass on the refusal path.

The vacuous-pass design across Layers 2-5 (not just 6) is deliberate:
a response that correctly refuses shouldn't be re-penalized for
"missing citations" or "no numeric data" — that's exactly what a
refusal communicates. The chain reports the refusal correctness as
the signal-of-record.

When SME questions land with non-empty `expected_entities`,
`expected_numeric_values` (with `expected_value`), or
`expected_refusal=False` + non-refusal responses, the validators
will exercise their grading logic against real signal.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_eval_validators.py
# → 45 passed in 0.20s

# Regression across all eval modules
docker exec georag-fastapi python -m pytest \
    tests/test_eval_validators.py \
    tests/test_workspace_evaluator.py \
    tests/test_real_llm_evaluator.py \
    tests/test_real_rag_evaluator.py \
    tests/test_evaluate_workspace_workflow.py
# → 69 passed in 5.92s

# Live full 6-layer chain
# → 8/8 pass on refusal_correctness via real_rag_v1

bash scripts/autonomous_run_substrate_verify.sh
# → 105/105 checks passed
```

## Cumulative session state — 37 ticks closed

- **Doc-phase ticks this run:** **37** (132 → 168)
- **Substrate verifier:** **105/105 PASS**
- **Live pytest cases:** 280 (262 + 18)
- **Sections closed:** §25.4 + §6 + **§04i validator surface**
- **§04i validators graduated:** **6 of 6 — COMPLETE**
- **Evaluator kinds wireable:** 3 (synthetic_stub + real_llm_v1 + real_rag_v1)
- **§21.3 types covered:** 8 of 8
- **Real RAG eval runs (full chain green):** 1 (8/8)
- **PublicGeo features on map:** 95

## What's actually graduated end-to-end now

### Read surfaces (4 admin dashboards, all live)
- Eval Dashboard — 5+ runs, 3 evaluator kinds badged, 53 golden questions
- Decision History — 9 decisions across 8 §21.3 types
- Support Cockpit — 6 tickets fully chained + 1 replay
- Hypothesis Workspace — 9 hypotheses + 27 evidence links

### Write surfaces (1)
- /admin/decisions/new — manual §21.3 decision entry

### Public-data surface (1)
- /public-geoscience map — 95 features across 5 tables, 4 jurisdictions

### Eval pipeline (3 evaluators × 6 §04i validators)
- synthetic_stub (always-pass with tag)
- real_llm_v1 (vLLM + Layer 6 refusal)
- real_rag_v1 (Qdrant + Neo4j + vLLM + all 6 §04i layers)

### Section closures (3)
- §25.4 support agents — 5 of 5
- §6 PublicGeo adapters — 9 of 9
- §04i validators — 6 of 6

### Cross-section integrations (1)
- §7.2 ↔ §9.13 (what_changed report uses detector output)

### Hatchet workflow graduations (6 of 11)
- evaluate_workspace, generate_report, score_targets, support_replay,
  what_changed_detector, restore_workspace

## What's next

The §04i surface is closed. Next directions:

- **Real silver data wiring** — ingest a project's drillholes/assays so
  Layer 3 has ground truth to compare against (`numeric_grounding`
  questions become real regression checks)
- **Embedding model + reranker into AgentDeps** — currently None →
  fallback retrieval. Real BGE + cross-encoder sharpens Layer 5
  resolution rate
- **§10.6 promotion-gate cron** — schedule nightly real_rag_v1 against
  refusal_correctness, alarm on regressions
- **SME-author core_chat / public_private_boundary / target_recommendation**
  question sets that exercise all 6 validators in non-vacuous mode

## Carry-overs

- All 5 layers (1, 2, 3, 4, 5) vacuous-pass on `expected_refusal=True`.
  That's the right behavior: refusal correctness is the signal of
  record; redundant penalties on a correct refusal would create
  false regressions.
- Layer 3's "structural-only specs" path covers the 15 seeded
  numeric_grounding questions that today have `path` but no
  `expected_value`. A subsequent graduation can wire silver data
  ground-truth derivation, switching those to checkable.
- Layer 1's default threshold (0.5) is identical to the chat-side
  gate. Per-question-set thresholds could tighten precious-metals /
  uranium grading vs. general-knowledge questions; tune via the
  §10.6 promotion gate config.
- The `_make_question()` test helper now accepts all four optional
  expected_* parameters, making future test additions trivial.
