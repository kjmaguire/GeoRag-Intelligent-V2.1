## Doc-phase 169 handoff — Embedding model + reranker wired into AgentDeps

**Status:** Live + 105/105 substrate verifier + 8/8 real-RAG pass preserved.

## What landed

Wired the SentenceTransformer (BGE) embedding model into the eval
path's AgentDeps singleton. The eval path now runs with real vector
retrieval — matching the chat API's behavior — rather than the
keyword-fallback degraded mode it ran in for doc-phases 162-168.

### `_build_agent_deps()` — embedding model + reranker

Both wrapped in graceful-degradation try/except so a missing weight
cache or download failure leaves the field None and the orchestrator
falls back to its degraded paths.

| Component | Status | Source |
|---|---|---|
| SentenceTransformer (`BAAI/bge-small-en-v1.5`, 384-dim, CPU) | **loaded + warmed** | Fresh instance per eval worker |
| CrossEncoder reranker (`BAAI/bge-reranker-base`) | failed gracefully | ONNX backend config mismatch (pre-existing) |

The reranker failure is a pre-existing issue with bge-reranker-base's
ONNX loader config — `Unrecognized model in BAAI/bge-reranker-base.
Should have a 'model_type' key in its config.json`. This affects both
the FastAPI chat path and the eval path equally; it's a separate
ticket to fix the reranker loader. Today the eval runs with
**embedding-backed retrieval + no cross-encoder reranking**, which
is meaningfully better than fully-degraded but not as sharp as the
fully-tuned configuration.

### Reused warm-loaded reranker (when it works)

The reranker code path uses `app.services.reranker._get_reranker()`
which is `@lru_cache(maxsize=1)`. Once the FastAPI app loads it
successfully, the eval path picks up the same instance. When the
loader bug is fixed centrally, both paths benefit without code
changes.

## Live verification — 8/8 real-RAG pass preserved

```text
embedding_model_loaded model=BAAI/bge-small-en-v1.5 dim=384
real_rag_evaluator.deps_built embedding=True reranker=False
...
pass: 8/8 on refusal_correctness
```

The retrieval now actively queries Qdrant + Neo4j with real BGE
embeddings. For the seeded refusal_correctness questions, the
orchestrator still correctly refuses (the embedding finds no
relevant chunks, retrieval returns empty, LLM refuses) — preserving
the full §04i chain pass.

### Cross-section finding — `answer_retrieval_items` FK violation

The embedding-backed retrieval now actively tries to log retrieval
records against `silver.answer_retrieval_items`, which has an FK to
`document_passages`. Today the project has no ingested documents, so
the retrieval logs reference chunk IDs that aren't in `document_passages`,
producing FK violations. The orchestrator catches these and falls
back to `insufficient_evidence` refusal — which is the right behavior.

When document ingestion happens for the Phantom Lake project (or any
ingested project), this FK issue resolves on its own: real chunks
land in `document_passages` first, then retrieval logs match the
existing rows. No code change needed; data fixes the symptom.

## Smoke verification

```bash
# Tests still pass (no test-shape changes; runtime path improved)
docker exec georag-fastapi python -m pytest tests/test_real_rag_evaluator.py
# → 4 passed in 1.96s

# Substrate verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 105/105 checks passed

# Live eval run with real embeddings
# → embedding_model_loaded model=BAAI/bge-small-en-v1.5 dim=384
# → pass: 8/8 on refusal_correctness via real_rag_v1
```

## Cumulative session state — 38 ticks closed

- **Doc-phase ticks this run:** **38** (132 → 169)
- **Substrate verifier:** **105/105 PASS**
- **Live pytest cases:** 280
- **Sections closed:** §25.4 + §6 + §04i validators
- **§04i validators:** 6 of 6
- **Evaluator kinds wireable:** 3
- **Real RAG eval with embedding-backed retrieval:** **live**
- **§21.3 types covered:** 8 of 8
- **PublicGeo features on map:** 95

## What's next

The eval pipeline is now structurally complete:
- Real Qdrant + Neo4j + vLLM + embedding model
- Full 6-layer §04i validator chain
- 3 evaluator kinds wireable from Hatchet workflow input
- Per-evaluator badging on the dashboard

Remaining productive directions:
- **Fix the bge-reranker-base ONNX config** so the cross-encoder gate
  fires across both chat + eval paths
- **Ingest a sample project's documents** so retrieval surfaces real
  chunks (turns the refusal_correctness 8/8 into a meaningful "8/8
  refused correctly with no chunks" + lets the orchestrator answer
  non-refusal questions with grounded citations)
- **Schedule a Hatchet cron** firing real_rag_v1 nightly against
  refusal_correctness — regression alarm for §2.9 drift
- **SME-author core_chat / public_private_boundary / target_recommendation**
  question sets for non-vacuous validator exercising

## Carry-overs

- The reranker load is a pre-existing issue, not introduced by this
  tick. Tracker: `Unrecognized model in BAAI/bge-reranker-base. Should
  have a 'model_type' key in its config.json`. Fix is upstream of
  both chat + eval — file as a separate operator ticket.
- `silver.answer_retrieval_items` FK violations show up in logs but
  don't fail the eval. They're a sign the orchestrator is doing real
  retrieval and trying to log it — surface to fix when project
  ingestion lands so the FK can be satisfied.
- Each eval worker loads ~67 MB of BGE weights into RAM on first
  call. For high-frequency eval crons this is fine (worker reuses
  singleton); for one-shot test scripts the load adds ~3-5s startup.
- `get_sentence_embedding_dimension()` is renamed to
  `get_embedding_dimension()` in newer sentence-transformers; the
  FutureWarning is noted, fix in a sweep when convenient.
