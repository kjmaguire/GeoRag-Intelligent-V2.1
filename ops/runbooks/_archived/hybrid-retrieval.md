# Hybrid Retrieval Runbook

Covers the dense + sparse model stack, identifier-boost logic, and the procedure for bumping any model version. Use this when changing a model pin, investigating recall gaps, or onboarding a new contributor to the retrieval stack.

---

## Current model pins

| Role | Model | Revision SHA | Version string | Runtime |
|---|---|---|---|---|
| Dense encoder | `BAAI/bge-small-en-v1.5` | (pinned via sentence-transformers, not SHA — Milestone 2 benchmark pending) | stored in `answer_runs.embedding_model_version` | CPU, FastAPI worker |
| Sparse encoder | `naver/splade-cocondenser-ensembledistil` | `49cf4c7b0db5b870a401ddf5e2669993ef3699c7` | `splade-cocondenser-ensembledistil@49cf4c7b` | CPU, FastAPI worker |
| Cross-encoder reranker | `BAAI/bge-reranker-base` | `5ccf1b81c57ff625b3e4b7ab15481d6e2ee9bc56` | `bge-reranker-base@5ccf1b81` | CPU, FastAPI worker |

All three models run on CPU in the `georag-fastapi` container. No GPU is allocated to FastAPI today. SPLADE takes the longest: ~50–100 ms/query on a warm cache (the `lru_cache` singleton means model weights are loaded once per worker process, not per request).

**Memory budget per FastAPI worker:**

| Component | Approx. footprint |
|---|---|
| SPLADE++ (fp32 CPU) | ~440 MB |
| BGE-small dense (fp32 CPU) | ~100 MB |
| BGE reranker (fp32 CPU) | ~278 MB |
| OS + Python overhead | ~200 MB |
| **Total (4 workers)** | **~4 × ~1 GB = ~4 GB** |

`georag-fastapi` container limit is set to `6g` in `docker-compose.yml`. Do not reduce below `5g`.

---

## How to bump a model version

All three models follow the same procedure. Steps for SPLADE shown as the canonical example.

### 1. Confirm the new revision SHA

```bash
# From the HuggingFace API — requires outbound HTTPS from your dev machine.
# Replace MODEL with the model repo slug.
curl -s "https://huggingface.co/api/models/naver/splade-cocondenser-ensembledistil" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['sha'])"
```

Record the full 40-character SHA. The short form is the first 8 characters.

### 2. Update the service module constant

**For SPLADE** — edit `src/fastapi/app/services/sparse_encoder.py`:

```python
SPARSE_MODEL_REVISION = "<new-40-char-sha>"
SPARSE_MODEL_VERSION  = "splade-cocondenser-ensembledistil@<first-8-chars>"
```

**KEEP IN SYNC**: also update the Dagster copy at `src/dagster/georag_dagster/assets/sparse_encoder.py`. Both files must be identical. The header comment on both files says "Last sync: YYYY-MM-DD" — update that date too.

**For the reranker** — edit `src/fastapi/app/services/reranker.py`:

```python
RERANKER_REVISION = "<new-40-char-sha>"
RERANKER_VERSION  = f"bge-reranker-base@{RERANKER_REVISION[:8]}"
```

**For the dense encoder** — the constant lives in `main.py` (lifespan hook) and whichever config key controls `embedding_model_version` written to answer_runs. Update both.

### 3. Bump retrieval_strategy_version

Edit `src/fastapi/app/services/query_classifier.py`:

```python
RETRIEVAL_STRATEGY_VERSION = "v1-hybrid-YYYY-MM-DD"   # use today's date
```

This changes the `rsv` component of every v4 cache key, producing a global cache miss. All cached answers will be re-fetched under the new model. This is the correct behavior: changed model weights = changed retrieval = stale cache entries must not be served.

### 4. For SPLADE or reranker: pre-warm the model after restart

The `lru_cache` singleton loads on first call. The FastAPI lifespan hook calls `_get_reranker()` and `encode_sparse()` at startup, so the model is warm before the first real request. After a container restart (not a hot-reload), wait for the lifespan to complete before sending traffic.

```bash
# Watch for the "loaded" log lines:
docker logs -f georag-fastapi 2>&1 | grep -E "SPLADE|reranker|loaded|ready"
# Expect:
#   Loading SPLADE++ model naver/splade-cocondenser-ensembledistil @ 49cf4c7b...
#   SPLADE++ loaded on CPU (fp32) -- expect 15-60ms per encode
#   Loading reranker: BAAI/bge-reranker-base revision=5ccf1b81...
#   Reranker ready: BAAI/bge-reranker-base (bge-reranker-base@5ccf1b81)
```

The models download from HuggingFace on first use if not in the container's `HF_HOME=/tmp/hf_cache`. In a fresh container expect a one-time download of ~440 MB (SPLADE) + ~278 MB (reranker).

### 5. Re-index documents with new SPLADE weights

If you change `SPARSE_MODEL_REVISION`, the existing sparse vectors in Qdrant were encoded with the old weights. Queries use the new weights — there is now a mismatch between index and query encoder. Old sparse vectors will return lower-quality matches for queries that rely on the new weight distribution.

Re-index procedure:
1. Run `index_reports` Dagster asset — recreates sparse vectors for `georag_reports` collection.
2. Run `index_public_geoscience_qdrant` Dagster asset — recreates sparse vectors for all `pg_*` collections.
3. Confirm points via Qdrant scroll: `parser_version` payload field should match the new `SPARSE_MODEL_VERSION`.

```bash
# Confirm parser_version on a sample point:
curl -s -X POST http://localhost:6333/collections/georag_reports/points/scroll \
  -H "Content-Type: application/json" \
  -d '{"limit": 1, "with_payload": true}' \
  | python3 -c "import sys,json; pts=json.load(sys.stdin)['result']['points']; print(pts[0]['payload'].get('parser_version','MISSING'))"
```

---

## Identifier-boost regex patterns

When `detect_identifiers()` fires, the sparse Qdrant prefetch limit widens from 100 to 150 (`SPARSE_BOOST_FACTOR = 1.5`). The dense branch stays at 100. This biases the RRF pool toward exact-token matches without ballooning total candidate count.

| Pattern class | Regex family | Example match | Example non-match |
|---|---|---|---|
| `HOLE_ID_DASHED` | Family A: `[A-Z]{2,6}-[A-Z0-9]{1,6}-\d{1,5}` — Family B: `\d{2,4}-[A-Z]{1,6}-\d{1,5}` | `PLS-22-08`, `23-MS-117`, `2024-DDH-001`, `AB-MS-005` | `2022-04-15` (date — no letter middle) |
| `HOLE_ID_COMPACT` | `[A-Z]{2,4}\d{2,5}` | `DDH0023`, `MS2024001` | `A1` (too short) |
| `SAMPLE_ID_ALPHA` | `[A-Z]{1,4}\d{4,8}` | `MS240301`, `AU123456` | `AU12` (too few digits) |
| `SAMPLE_ID_DASHED` | `[A-Z]{2}-\d{6}` | `AU-240301`, `CU-123456` | `AU-2403` (too few digits) |
| `NTS_TILE` | `\d{2,3}[A-P]\d{2}` | `74I12`, `104B08` | `74Q12` (Q is outside A–P) |
| `COMMODITY_CODE` | Exact-match frozenset (case-sensitive) | `Au`, `U3O8`, `REE`, `Cu` | `au` (lowercase — no match) |

The commodity frozenset: `{Au, Ag, Cu, Pb, Zn, Ni, Co, Mo, U, U3O8, REE}`.

All patterns are compiled once at import time in `src/fastapi/app/services/identifier_boost.py`. The boost is applied unconditionally when any pattern fires (Global Invariant 11 — default-on).

---

## Workspace-level override

Identifier boost is currently default-on for all workspaces. There is no per-workspace disable path.

**TODO (Module 9 scope):** When `workspace_settings` table gains an `identifier_boost_enabled` boolean column, the orchestrator should call `detect_identifiers()` only when that flag is TRUE (or absent, defaulting to TRUE). The `identifier_boost.py` module's `get_patterns()` function already documents this hook point.

A future workspace admin would disable it via:

```sql
-- Once workspace_settings exists (Module 9):
UPDATE silver.workspace_settings
SET identifier_boost_enabled = false
WHERE workspace_id = '<uuid>';
```

Until then, the only way to suppress boost is to remove a pattern class from `identifier_boost.py` globally — do not do this without Kyle's approval.

---

## Hybrid vs dense-only: empirical rationale

No formal Phase C benchmarking has run yet (deferred pending Module 10 golden-corpus assembly and background materialize `778c604c` completion). The empirical argument for keeping hybrid on:

- SPLADE++ activates on geological identifier tokens — exact strings like `PLS-22-08`, `74I12`, `U3O8` — that dense cosine similarity struggles with because they are rare in pretraining corpora.
- A query for "what are the assay results for PLS-22-08" contains the hole ID as the primary retrieval signal. Dense cosine will find documents about assay methodology in general; sparse exact-match finds the specific hole's data first.
- Identifier-heavy queries make up a substantial fraction of real geological workload (field geologists querying by hole ID daily).

Phase C will quantify this on the golden-corpus set (recall@10, recall@20, MRR, and latency p95). Results will be surfaced to Kyle for final model-selection approval.

---

## Fallback behavior on model failure

**SPLADE encoder failure:**

Per `qdrant_service.py` docstring (GI-11 comment): if `encode_sparse()` raises, the query fails. There is no dense-only fallback. Hybrid retrieval is a core V1 invariant. The orchestrator surfaces the error to the user rather than silently degrading.

**BGE reranker failure:**

If `get_reranker_or_none()` returns `None` (load failure) or the reranker times out (>2.0s), the orchestrator logs at WARNING and continues with RRF-ranked results. The reranker is a quality enhancement — its failure degrades precision but does not fail the query. `answer_runs.reranker_version` is still written with the expected version string for traceability; the `reranked` stage rows in `answer_retrieval_items` will be absent for that run.

**Model not cached (fresh container):**

First request after a container restart will block while the model downloads/loads from HuggingFace cache. Expect 10–30s for reranker (278 MB) if `HF_HOME=/tmp/hf_cache` is cold. SPLADE (440 MB) is similar. Pre-warm by hitting `/v1/health` immediately after container start and waiting for the "ready" log lines before routing user traffic.

---

*Written 2026-04-21 during Module 4 Phase D. Update whenever the underlying procedure changes.*
