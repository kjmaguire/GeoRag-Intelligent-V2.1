# Qwen3-Embedding Cutover (bge-small → Qwen3-Embedding-0.6B)

**Status:** Ready to execute — 2026-06-04
**Author:** Architectural audit follow-up
**Pairs with:** [llm-model-swap.md](llm-model-swap.md), [qdrant-snapshot.md](qdrant-snapshot.md)

## What this does

Replaces `BAAI/bge-small-en-v1.5` (384-dim) with `Qwen/Qwen3-Embedding-0.6B`
(1024-dim) in the `georag_chunks` Qdrant collection. The dim change
requires the collection be **dropped and recreated** — in-place UPSERT
is impossible across dim boundaries (Qdrant rejects with HTTP 400
"Wrong vector size").

The source of truth for re-embed is `silver.document_passages` (which
holds the chunk text + metadata). After cutover the collection is
re-populated from there via the `embed_pending_passages` workflow.

## Why now

Per the 2026-06-04 architectural audit:
- Code at `app/main.py`, `app/services/reranker.py`, `app/agent/deps.py`
  already references `Qwen/Qwen3-Embedding-0.6B`
- `app/config.py` `EMBEDDING_MODEL_NAME` updated to match
- Until the collection is re-embedded at 1024-dim, **every chat query
  returns HTTP 400** — Qdrant's hybrid query rejects the 1024-dim query
  vector against the 384-dim collection

## Pre-flight (already done 2026-06-04)

- [x] **Snapshot created** of the existing 384-dim collection:
      `georag_chunks-7722704038637137-2026-06-04-16-41-38.snapshot`
      (91MB) — rollback insurance
- [x] **Qwen3-Embedding model verified** loading in container (38s cold
      load, produces 1024-dim normalized vectors)
- [x] **Qwen3-Reranker model verified** (loads in 30s; observed score
      range -11.6 to +9.2 on a 3-pair smoke; sign convention preserved)
- [x] **Config + script updates committed** (pending PR — see "Files
      changed" below)

## Execution sequence

Steps must run **in order**. Each step has a verification check —
do not proceed if a check fails.

### 1. Final pre-cutover snapshot

```bash
# Take a fresh snapshot just before drop (in case ingestion happened
# since the 2026-06-04 snapshot). Returns the snapshot filename;
# capture it for rollback.
SNAPSHOT=$(curl -s -X POST "http://localhost:6333/collections/georag_chunks/snapshots" \
  | python -c "import sys,json; print(json.load(sys.stdin)['result']['name'])")
echo "Pre-cutover snapshot: $SNAPSHOT"

# Verify it landed on disk + capture size
curl -s "http://localhost:6333/collections/georag_chunks/snapshots" | python -m json.tool | head -20
```

### 2. Mark all passages as pending re-embed

```bash
# Reset embedding_id so embed_pending_passages picks up everything.
# Inside a transaction so a mid-step failure rolls back.
docker exec georag-postgresql psql -U georag -d georag -c "
BEGIN;
SELECT count(*) AS passages_to_reembed FROM silver.document_passages WHERE embedding_id IS NOT NULL;
UPDATE silver.document_passages SET embedding_id = NULL;
COMMIT;
"
```

**Verification:** the SELECT count printed above is the number of
passages that need re-embedding. Should match the points_count of the
old `georag_chunks` collection (within ingest-since-snapshot drift).

### 3. Drop + recreate `georag_chunks` at 1024-dim

```bash
# Drop the old 384-dim collection
curl -X DELETE "http://localhost:6333/collections/georag_chunks"

# Recreate at the new dim (init_qdrant.py reads GEORAG_VECTOR_SIZE
# env var; the script already defaults to 1024 per the 2026-06-03
# Qwen3 sizing, so no env override needed)
docker exec georag-fastapi python /app/scripts/init_qdrant.py
```

**Verification:**
```bash
curl -s "http://localhost:6333/collections/georag_chunks" \
  | python -c "import sys,json; d=json.load(sys.stdin); print('dim:', d['result']['config']['params']['vectors'].get('size', d['result']['config']['params']['vectors'].get('', {}).get('size')))"
```
Should print `dim: 1024`.

### 4. Trigger the embed sweep

```bash
# Dispatches the EmbedPendingPassages workflow against every
# silver.document_passages row (workspace_id=DEFAULT scopes to all).
# Hatchet runs the embed in batches via the embed-pending-passages
# workflow.
docker exec georag-fastapi python -c "
import asyncio
from app.hatchet_workflows.embed_pending_passages import EmbedPendingPassagesInput, embed_pending_passages_wf
from app.hatchet_workflows._workspace_input import bootstrap_workspace_id

async def main():
    inp = EmbedPendingPassagesInput(
        workspace_id=bootstrap_workspace_id(reason='dagster.nightly_embed'),
        project_id='*',
        batch_size=64,
    )
    print('dispatching...')
    # Direct invocation pattern — the workflow processes ALL pending passages
    # for the workspace. project_id='*' means cross-project.
    from app.services.ingest.passage_embedder import embed_pending_passages
    result = await embed_pending_passages(
        workspace_id=inp.workspace_id,
        project_id=None,
        batch_size=inp.batch_size,
    )
    print(f'result: {result}')

asyncio.run(main())
"
```

**Live monitoring (separate terminal):**
```bash
# Watch points_count climb in the new collection
watch -n 5 'curl -s http://localhost:6333/collections/georag_chunks | python -c "import sys,json; print(\"points:\", json.load(sys.stdin)[\"result\"][\"points_count\"])"'
```

Expected throughput per memory notes:
- CPU: ~3-4 chunks/sec → ~9099 chunks ≈ 40-50 min
- GPU (hatchet-worker-ai with A4500): ~144 chunks/sec → ~9099 chunks ≈ 65 sec

### 5. Verify

```bash
# Counts match (within ingest drift)
docker exec georag-postgresql psql -U georag -d georag -c "
SELECT count(*) AS silver_passages FROM silver.document_passages WHERE embedding_id IS NOT NULL;
"
curl -s "http://localhost:6333/collections/georag_chunks" \
  | python -c "import sys,json; print('qdrant points:', json.load(sys.stdin)['result']['points_count'])"

# Sanity query end-to-end
docker exec georag-fastapi python /app/scripts/run_eval_120.py
```

Commit the eval output to `ops/baselines/qwen3-embedding-cutover-2026-06-04.md`.

### 6. Health check (15-min watch)

- Watch `/metrics` for `georag_queries_total{outcome="empty"}` rate (should
  drop to baseline within 5 min as warm queries hit the new collection)
- Watch Loki for `"Wrong vector size"` errors (should be 0)
- Watch Hatchet UI for embed-pending-passages re-run failures

## Rollback

```bash
# 1. Restore from the pre-cutover snapshot
curl -X PUT "http://localhost:6333/collections/georag_chunks/snapshots/recover" \
  -d '{"location":"file:///qdrant/snapshots/georag_chunks/<SNAPSHOT_NAME>"}'

# 2. Revert app/config.py + app/main.py + app/services/reranker.py to
#    the pre-swap commits (see PR pr/08 + pr/11 + config commits)
git revert <commit-sha>
docker compose restart fastapi hatchet-worker-ai

# 3. Re-baseline against the old model via the prior baseline file
```

## Files changed in this cutover

- `src/fastapi/app/config.py` — `EMBEDDING_MODEL_NAME`, `RERANKER_MODEL_NAME`,
  `RERANKER_SCORE_THRESHOLD` docstring updates
- `src/fastapi/scripts/reembed_qdrant.py` — model swap + dim guard
- `src/fastapi/scripts/run_eval_120.py` — restored to canonical path
- `ops/runbooks/qwen3-embedding-cutover.md` — this file

## Why we didn't execute step 3+ overnight

The drop-recreate sequence is destructive: a mid-step failure with
nobody watching can produce an empty collection that takes ~50 min on
CPU to re-fill. Kyle is awake at 10am to run step 3 onwards while
watching the Hatchet UI + Qdrant points_count climb. Steps 1-2 (snapshot
+ pre-flight verification) DID run overnight — collection snapshot taken
at 2026-06-04 16:41:38 (91 MB), both Qwen3 models verified loading +
producing correct dims, score range confirmed sign-convention-preserved.
