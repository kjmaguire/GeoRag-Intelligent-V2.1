# Runbook — Embedding swap: bge-small-en-v1.5 → Qwen3-Embedding-0.6B

**Status:** Code is migrated as of 2026-06-03. **Corpus re-embed has NOT
been executed.** Until the steps below run, queries will 400 against
existing Qdrant collections because the vector dim changed 384 → 1024.

## Why

- **Quality:** Qwen3-Embedding-0.6B beats bge-small-en-v1.5 by a wide
  margin on MTEB-retrieval (and beats bge-large in most categories).
  Pairs naturally with the Qwen3-Reranker-0.6B (same model family,
  shared tokenizer distribution).
- **Asymmetric encoding:** Qwen3-Embedding applies a query-side
  instruction template ("Instruct: ...\nQuery: ...") via
  `prompt_name="query"`. Documents are encoded raw. The +1-5% MTEB lift
  vs raw encoding is essentially free if the writers stay raw and the
  query path uses the prompt_name.
- **Native 32K context** for the encoder (vs bge-small's 512 cap).

## Pre-flight

1. Verify the `Qwen/Qwen3-Embedding-0.6B` weights downloaded into the
   FastAPI image. On first lifespan boot, the model auto-downloads to
   the HF cache (~1.2 GB).
2. Verify `settings.EMBEDDING_DIMENSION == 1024` in the running
   container: `docker exec georag-fastapi python -c
   "from app.config import settings; print(settings.EMBEDDING_DIMENSION)"`.
3. Confirm Qdrant is healthy: `curl http://qdrant:6333/healthz`.
4. **Backup the existing collections** before the destructive recreate:
   `qdrant-cli snapshot create georag_chunks` (or use the Qdrant HTTP
   `POST /collections/{name}/snapshots` API).

## Migration

The dimension change INVALIDATES every existing vector. The collections
must be dropped and recreated; the corpus must be re-embedded from the
silver-tier source rows.

```bash
# 1. Stop ingestion (no new vectors during the migration).
docker compose -p georagintelligencev10 --env-file .env stop \
  hatchet-worker-ai dagster-daemon dagster-webserver

# 2. Drop the old 384-dim collections.
docker exec georag-qdrant sh -c '
  curl -X DELETE http://localhost:6333/collections/georag_chunks
  curl -X DELETE http://localhost:6333/collections/georag_reports
'

# 3. Recreate at 1024-dim. The init script reads GEORAG_VECTOR_SIZE.
docker exec georag-fastapi env GEORAG_VECTOR_SIZE=1024 \
  python /app/scripts/init_qdrant.py

# 4. Restart workers.
docker compose -p georagintelligencev10 --env-file .env start \
  hatchet-worker-ai dagster-daemon dagster-webserver

# 5. Trigger the full corpus re-embed via Dagster. The `embed_pending`
#    asset reads silver.document_passages and writes points to
#    georag_chunks; setting embedding_id = NULL forces a re-encode.
docker exec georag-postgresql psql -U georag -d georag -c "
  UPDATE silver.document_passages SET embedding_id = NULL;
"

# Tail the worker logs to confirm dense encode is running at 1024 dim:
docker logs -f georag-hatchet-worker-ai | grep -E "embed_pending|dim="
```

## Verification

1. **Dim assertion** in lifespan logs at startup:
   ```
   Embedding model ready: Qwen/Qwen3-Embedding-0.6B (dim=1024)
   ```
2. **Re-embed completion:** count of NULL `embedding_id` rows drops to 0
   (or the rejected-row count, which should be small):
   ```sql
   SELECT COUNT(*) FILTER (WHERE embedding_id IS NULL) AS pending,
          COUNT(*)                                      AS total
   FROM silver.document_passages;
   ```
3. **One real query** end-to-end. Open Chat, ask a known-good question
   from the gap-question set, confirm citations come back. A 400 on the
   Qdrant search step means the collection dim mismatch wasn't fully
   resolved.
4. **Golden-query NDCG re-baseline.** Run
   `scripts/run_eval_120.py` and compare NDCG@10 / Recall@20 against
   the pre-swap baseline stored in `bench_results_to_commit_baseline.json`.

## Rollback

If the re-embed fails or quality regresses below the baseline:

1. Re-enable the bge model via env override:
   ```
   EMBEDDING_MODEL_NAME=BAAI/bge-small-en-v1.5
   EMBEDDING_DIMENSION=384
   EMBEDDING_QUERY_PROMPT_NAME=  # empty → raw encoding
   ```
2. Restore the pre-swap Qdrant snapshots (step 4 of pre-flight).
3. Restart FastAPI and Hatchet workers.

## Why the lifespan asserts the dim

A silent mismatch (e.g. operator sets `EMBEDDING_MODEL_NAME` to a
different-dim model but forgets `EMBEDDING_DIMENSION`) used to surface
as a 400 on every Qdrant upsert. The 2026-06-03 swap adds a fail-fast
assertion in `main.py` lifespan so the wrong model fails to boot
instead of corrupting ingest.

## See also

- [vLLM polish 2026-06-03 memory](../../.claude/projects/C--Users-GeoRAG/memory/project_vllm_polish_2026_06_03.md)
- [Reranker overnight 2026-05-29 memory](../../.claude/projects/C--Users-GeoRAG/memory/project_reranker_overnight_2026_05_29.md)
  (why bge-FT was parked → why Qwen3-Reranker swap matters here too)
- [docs/RUNBOOK.md § "Recreate a Qdrant collection"](../RUNBOOK.md)
