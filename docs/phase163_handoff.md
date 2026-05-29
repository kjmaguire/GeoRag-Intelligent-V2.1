## Doc-phase 163 handoff — §04i Layer 2 citation-presence validator + shared validators module

**Status:** Live + 20/20 unit tests + 40/40 regression + 8/8 real-RAG pass. **105/105 substrate verifier**.

## What landed

Second §04i validator graduates: **Layer 2 citation presence**. Plus
a shared `validators` module that both `real_llm_v1` and `real_rag_v1`
draw from.

### New module — `app/services/eval/validators.py` (~190 lines)

Centralizes the §04i validator implementations so future graduations
(Layers 1, 3, 4, 5) drop into one file. Exports:

- `REFUSAL_PATTERNS` — 21 canonical refusal phrases (was inline in
  real_llm_evaluator.py; moved here)
- `detect_refusal(text)` — case-insensitive substring matcher
- `ValidatorOutcome` NamedTuple — `(layer, passed, detail, failure_message)`
- `validate_refusal_correctness(...)` — §04i Layer 6 / §2.9 (doc-phase 159)
- `validate_citation_presence(...)` — §04i Layer 2 / typed output (new doc-phase 163)
- `chain_validators(outcomes)` — AND-semantics combiner; first failing
  outcome's layer + message wins

### Layer 2 semantics

Per master-plan §04i and CLAUDE.md hard-rule #4: "Citations are
mandatory on every RAG response. Every claim the LLM makes must
include a source_chunk_id or be rejected by Pydantic AI's typed
output validation."

Validator behavior:

| Question state | Response state | Outcome |
|---|---|---|
| `expected_refusal=True` | any | **vacuous pass** — refusal exempts citation requirement |
| `expected_refusal=False` | `citations=[]` | **fail** — Layer 2 violation, zero citations |
| `expected_refusal=False` + `expected_citations=[…N]` | `len(citations) < N` | **fail** — not enough citations |
| `expected_refusal=False` + `expected_citations=[]` | `len(citations) >= 1` | **pass** |
| `expected_refusal=False` + `expected_citations=[…N]` | `len(citations) >= N` | **pass** |

### `real_rag_v1` now chains 2 validators

`evaluate_question_real_rag` builds a list of `ValidatorOutcome`s
and runs `chain_validators` to AND them together. `actual_payload`
gets a `validator_outcomes` array with per-validator breakdown for
debugging.

```python
outcomes = [
    validate_refusal_correctness(response_text=..., question=...),
    validate_citation_presence(citations=..., question=...),
]
all_passed, failure_layer, failure_detail = chain_validators(outcomes)
```

The `failure_layer` namespacing changed from `'refusal'` to
`'6_refusal'` and adds `'2_citation_presence'`. Downstream consumers
(promotion gate, dashboard) read the bucketed layer string.

### `real_llm_v1` — backward-compat refactor

`_REFUSAL_PATTERNS` + `_detect_refusal` in `real_llm_evaluator.py`
are now re-exports of the shared module. Existing test imports
continue working without changes.

`real_llm_v1` still applies only Layer 6 (no retrieval → no citations
to validate). When/if a vLLM-with-citations format graduates, it can
opt into Layer 2.

## Tests — 20 new + 20 regression

`src/fastapi/tests/test_eval_validators.py` — **20 pytest cases:**

Refusal detection (5):
- canonical phrases, orchestrator phrases, normal-answer, empty, REFUSAL_PATTERNS export

Refusal validator (4):
- pass when refused as expected (+/- two directions)
- fail when refused unexpectedly + fail when answered unexpectedly

Citation validator (6):
- vacuous-pass on refusal
- fail on non-refusal with zero citations
- pass with ≥1 citation
- pass with many citations
- fail when `expected_citations` count not met
- pass when meeting expected count

Chain validators (4):
- all pass; first failure short-circuits; late failure caught; empty list

**40/40 regression tests pass** across all eval modules.

## Live verification — 2 chained validators

Re-ran `real_rag_v1` on refusal_correctness with both validators active:

```text
pass_count: 8/8
success:    True
```

The expected-refusal questions all hit the vacuous-pass branch on
Layer 2 (refusal exempts citations), and Layer 6 catches the refusal
text. AND-chained: all 8 pass cleanly.

For future runs where `expected_refusal=False` and the LLM tries to
answer without citations, Layer 2 will catch it where Layer 6 alone
wouldn't. That's the new regression-detection coverage this tick adds.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_eval_validators.py
# → 20 passed

# Regression across all eval modules
docker exec georag-fastapi python -m pytest \
    tests/test_eval_validators.py \
    tests/test_real_llm_evaluator.py \
    tests/test_real_rag_evaluator.py \
    tests/test_workspace_evaluator.py
# → 40 passed in 5.44s

bash scripts/autonomous_run_substrate_verify.sh
# → 105/105 checks passed
```

## Cumulative session state — 32 ticks closed

- **Doc-phase ticks this run:** **32** (132 → 163)
- **Substrate verifier:** **105/105 PASS**
- **Live pytest cases:** 255 (235 + 20)
- **Sections closed:** §25.4 + §6
- **§04i validators graduated:** **2 of 6** (refusal_correctness + citation_presence)
- **Evaluator kinds wireable end-to-end:** 3 (synthetic_stub + real_llm_v1 + real_rag_v1)
- **Validator-chaining infrastructure:** Live (`chain_validators` + `ValidatorOutcome`)
- **§21.3 types covered:** 8 of 8
- **Real-RAG eval runs (green):** 1 (8/8 refusal_correctness with both validators)

## What's next

Each remaining §04i validator is a one-tick graduation that
appends to the chain:

- **Doc-phase 164** — Layer 3 numeric-claim validator (needs real
  silver data for ground-truth comparison)
- **Doc-phase 165** — Layer 4 entity-resolution validator (extract
  entity mentions, cross-check against `expected_entities`)
- **Doc-phase 166** — Layer 5 chunk-provenance validator (citations'
  source_chunk_ids resolve to real chunks in Qdrant)
- **Doc-phase 167** — Layer 1 retrieval-quality validator (citation
  relevance scores above gate threshold)

Or pivot:
- Wire embedding_model + reranker into AgentDeps (currently None)
- SME-author core_chat / public_private_boundary question sets
- Schedule nightly cron firing real_rag_v1 on refusal_correctness

## Carry-overs

- The `failure_layer` namespace changed from `'refusal'` → `'6_refusal'`.
  Any consumer that string-matches on the old name needs updating.
  Audit ledger payloads from doc-phase 159 runs carry the old name;
  new runs carry the namespaced version. Both stay readable.
- `validate_citation_presence` is conservative on `expected_citations`:
  it only checks count, not chunk-id match. Layer 5 (chunk-provenance)
  is where source_chunk_id ↔ Qdrant lookup happens.
- The `vacuous_pass_refusal_path` flag on detail surfaces the
  "Layer 2 doesn't apply" case explicitly, so reviewers can tell
  which questions got a free pass on citations vs which earned it.
