## Doc-phase 176 handoff — bge-reranker-base load failure root-caused + fixed (revision SHA re-pin)

**Status:** Live + reranker loads cleanly + AgentDeps now carries both `embedding_model` AND `reranker` for eval path + 4/4 real_rag_evaluator tests preserved.

## Root cause

The reranker load failure traced through doc-phases 162-169 was not
an ONNX backend config issue (as initially diagnosed) — it was a
**stale HuggingFace cache marker for an upstream-deleted revision SHA**.

The reranker code pinned to revision SHA
`5ccf1b81c57ff625b3e4b7ab15481d6e2ee9bc56` (confirmed 2026-04-21).
At some point between then and now, BAAI/bge-reranker-base was
either force-pushed or that revision was rebased away. The HF cache
detected the absent revision, wrote a `.no_exist/config.json` marker
file, and from that point forward sentence-transformers 5.5.0's
load path:

1. Sees no `modules.json` → falls back to AutoConfig.from_pretrained
2. AutoConfig.from_pretrained → cache lookup hits `.no_exist/config.json`
3. Cache returns "file does not exist" verdict
4. AutoConfig raises `ValueError: Unrecognized model in BAAI/bge-reranker-base.
   Should have a 'model_type' key in its config.json`

The error message was misleading — the issue wasn't the
`model_type` key being missing from a real config.json, it was that
HF had cached the fact that no config.json existed for that
specific SHA at all.

## The fix

Three steps:

1. **Cleared the `.no_exist` cache directory** for the model:
   ```
   docker exec georag-fastapi rm -rf \
     /tmp/hf_cache/models--BAAI--bge-reranker-base/.no_exist
   ```

2. **Re-pinned to the current main HEAD SHA**:
   - Old: `5ccf1b81c57ff625b3e4b7ab15481d6e2ee9bc56` (no longer accessible)
   - New: `2cfc18c9415c912f9d8155881c133215df768a70` (confirmed 2026-05-14)

3. **Updated `RERANKER_VERSION` accordingly**:
   ```
   bge-reranker-base@5ccf1b81  →  bge-reranker-base@2cfc18c9
   ```

   This version string lands in `answer_runs.reranker_version` on
   every reranker-gated answer; the SHA shift is now visible in the
   audit trail.

## Live verification

```python
>>> from app.services.reranker import _get_reranker
>>> m = _get_reranker()
>>> m.predict([("uranium mineralization in sandstone",
...             "the host rock is sandstone hosting uranium mineralization")])
[0.9917468]   # high-relevance pair scored 0.99
>>> m.predict([("uranium mineralization in sandstone",
...             "the cake is in the refrigerator")])
[0.0003]      # irrelevant pair scored near zero
```

```python
>>> deps = await _build_agent_deps()
>>> type(deps.embedding_model).__name__   # 'SentenceTransformer'
>>> type(deps.reranker).__name__          # 'CrossEncoder'
```

The eval path now has the **complete** §04i Layer 5 cross-encoder
gate. Previously (doc-phases 169-175) Layer 5 ran with embedding-only
retrieval + no rerank sharpening; now it has both.

## Smoke verification

```bash
# Real RAG evaluator regression
docker exec georag-fastapi python -m pytest tests/test_real_rag_evaluator.py
# → 4 passed in 2.05s

# AgentDeps construction confirms both models present
docker exec georag-fastapi python -c '...'
# → embedding_model: SentenceTransformer
# → reranker: CrossEncoder

# Substrate verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 112/112 checks passed
```

## Implications for the §04i chain

Layer 5 (chunk_provenance) now operates with the design-intent gate:

| Layer 5 component | Pre-doc-phase 176 | Post |
|---|---|---|
| Embedding retrieval | BGE-small (working) | BGE-small (unchanged) |
| Cross-encoder rerank | None (graceful-degraded) | **bge-reranker-base ACTIVE** |
| Min relevance threshold | 0.5 (RRF score only) | 0.5 + reranker top-k filter |

For the seeded refusal_correctness questions, the orchestrator
correctly refuses because retrieval returns no chunks — Layer 5
still vacuous-passes on the refusal path (no chunks to rerank). The
sharpening effect lights up when ingested documents land and
non-refusal questions get exercised.

The chat path (`/api/v1/chat/...`) also picks up the working
reranker via the same `_get_reranker()` singleton — both paths
benefit without further code change.

## Cumulative session state — 44 ticks closed

- **Doc-phase ticks this run:** **44** (132 → 176)
- **Substrate verifier:** **112/112 PASS**
- **Live pytest cases:** 286
- **Track3 dashboard tests:** 14/14 PASS
- **§04i validators:** 6 of 6 graduated + Layer 5 cross-encoder now ACTIVE
- **§10.6 alarm-loop:** emits to audit ledger on regression
- **Hatchet AI pool:** 12 workflows
- **Phase A ingestion:** staging at 35% (200GB → container-local, ETA ~20 min)

## Carry-overs

- The HF revision SHA is now pinned to a specific tip. If upstream
  rebases again, this will recur. Two long-term options:
  1. Mirror the model into our own SeaweedFS/HF cache and pin against
     that internal copy (immune to upstream rebases)
  2. Switch to a `cross-encoder/ms-marco-*` family model with more
     stable revision policy
  Today's pin is good enough for ~6 months barring HF history surgery.
- `RERANKER_VERSION` is now `bge-reranker-base@2cfc18c9`. Any prior
  `answer_runs.reranker_version` rows still show the old SHA — those
  reflect historic chat answers that ran with the degraded path.
  Don't backfill — the audit value of "what reranker actually ran"
  is preserved by leaving old rows alone.
- Sentence-transformers 5.5.0 deprecation warnings appear on every
  load (`cache_dir` arg, `TRANSFORMERS_CACHE` env). Non-blocking; fix
  in a sweep when convenient.
- `embedding_model.get_sentence_embedding_dimension()` → renamed
  `get_embedding_dimension()` in newer sentence-transformers. Same
  sweep candidate.
