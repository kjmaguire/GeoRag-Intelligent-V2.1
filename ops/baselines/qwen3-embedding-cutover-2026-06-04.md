# Qwen3-Embedding + Qwen3-Reranker Cutover Baseline (2026-06-04)

**Cutover status:** EMBED IN PROGRESS (live as of doc commit time)
**Production state at commit:** 9,099 silver passages being re-embedded
into 1,024-dim `georag_chunks` collection with Qwen/Qwen3-Embedding-0.6B
**Embed rate:** ~30 passages/min on CPU (single-process)
**ETA at this rate:** ~5 hours from cutover start (16:41 UTC → ~21:41 UTC)

## Eval baseline

**STATUS: PENDING** — `scripts/run_eval_120.py` to be run after embed
hits 100% (silver.document_passages.embedding_id IS NOT NULL on all
9,099 rows + `georag_chunks` `points_count` matches).

When the eval runs, the output block from `run_eval_120.py` will be
committed below this line as the post-swap baseline. The pre-swap
baseline (bge-small-domain-ft + bge-reranker-base) lives in the prior
session memory at `project_reranker_overnight_2026_05_29`.

### Eval invocation

```bash
docker exec georag-fastapi python /app/scripts/run_eval_120.py
```

### Eval output (will be appended)

```
PENDING — embed at __% completion as of __:__ UTC
```

## Cutover learnings (real bugs the runbook didn't predict)

The cutover exposed three issues that weren't in the pre-flight checklist:

### 1. `.env` overrides `config.py`

`config.py` having `EMBEDDING_MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"`
did NOT cause the swap. The `.env` file had
`EMBEDDING_MODEL_NAME=/app/models/bge-small-domain-ft` which won at
runtime. **Production cutover requires editing `.env`, not just
`config.py`.** Updated `.env` line 975 to point at Qwen3.

### 2. Container restart vs recreate

`docker compose restart fastapi` preserves the existing env vars (set
at first `up`). Updating `.env` requires `docker compose up -d
--force-recreate fastapi` to pick up the new values. Restart-without-recreate
keeps the OLD bge model loaded even though the file changed.

### 3. Two missing Settings fields in `config.py`

`main.py:587` reads `settings.EMBEDDING_QUERY_PROMPT_NAME`; `main.py:596`
reads `settings.EMBEDDING_DIMENSION`. Neither existed in `config.py`'s
Settings class. Without them, the embedding model load raises
`AttributeError` and the container starts but `app.state.embedding_model
= None`, so search returns empty results silently. Both added in this
session — see config.py line 866-872.

### 4. Sparse vector slot missing in init_qdrant.py

`init_qdrant.py` creates collections with only a single unnamed dense
vector slot. But `app/services/ingest/passage_embedder.py:220-224`
upserts to a dict that includes a NAMED sparse vector `"text"` for
SPLADE++. The recreate-via-init_qdrant left the collection without
that named sparse slot, so every UPSERT failed with `Not existing
vector name error: text`. **Real fix:** the cutover used a direct
`curl PUT /collections/georag_chunks` with both dense + sparse
vectors_config. init_qdrant.py needs a follow-up commit to declare
the sparse vector schema natively.

### 5. Production was running bge-small-domain-ft (a fine-tune)

Pre-cutover production was NOT vanilla `bge-small-en-v1.5` — it was a
LOCAL domain fine-tune at `/app/models/bge-small-domain-ft` (per the
reranker-v1 work memory). Swapping to Qwen3-Embedding-0.6B (stock)
discards the domain FT. Whether the eval improvement justifies losing
the FT is the post-cutover decision point.

## Pre-cutover snapshots (rollback insurance)

- `georag_chunks-7722704038637137-2026-06-04-16-41-38.snapshot` (91 MB) — overnight snapshot
- `georag_chunks-7722704038637137-2026-06-04-17-07-40.snapshot` (additional) — immediately before drop

Rollback procedure: see [ops/runbooks/qwen3-embedding-cutover.md §Rollback](../runbooks/qwen3-embedding-cutover.md#rollback).

## What's changed in production right now

- `app.state.embedding_model`: Qwen/Qwen3-Embedding-0.6B (1024-dim)
- `app.state.reranker`: qwen3-reranker-0.6b@main
- `georag_chunks` collection: 1024-dim dense + `text` sparse, RE-EMBED IN PROGRESS
- `silver.document_passages.embedding_id`: NULL for all rows; populated as embed lands
- Chat queries: WILL RETURN EMPTY until embed completes (workspace_id filter + new dim collection = no matches against old query vectors)
