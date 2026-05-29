# Appendix G — RAG Retrieval Contract

Status: **Draft.** Defines every numeric and structural parameter on the
retrieval path. The flag-gated agentic LangGraph (`AGENTIC_RETRIEVAL_V2_ENABLED`)
is the implementing surface — see [Ch 06](../manual/06-retrieval-and-agents.md).

## 1. Chunking rules

Documents → passages.

| Source | Chunker | Size | Overlap | Notes |
|---|---|---|---|---|
| PDF body text (silver.report_pages) | section-aware chunker in `src/dagster/georag_dagster/parsers/pdf_report.py` (`_section_to_chunks`) | target ~1500 chars (max 2000) | 200 chars rolling | Hard breaks at H1/H2 headings; never split a sentence; never split a table row |
| PDF tables (silver.report_tables) | one passage per table | n/a | n/a | Table serialised as Markdown + a key-value sidecar in payload |
| PDF figures (silver.report_figures) | one passage per figure caption + VL description | n/a | n/a | When VL pass on, `vl_description` concatenated to caption |
| Silver row records (lithology / assay / log) | one passage per row | n/a | n/a | Row → "Hole {hole_id}, {from}-{to} m: {rock_code} ({description})" template |
| Public geoscience features | one passage per feature | n/a | n/a | "Mine {name} in {region}, commodity {primary}" template |

The chunker writes to `silver.document_passages.text` and writes the
passage UUID into the payload before embedding.

## 2. Embedding model

- Model: `BAAI/bge-small-en-v1.5` (sentence-transformers).
- Dim: **384**.
- Distance: **Cosine**.
- Normalise: yes (model emits L2-normalised vectors).
- Batch size at index time: 32 (constrained by GPU mem with reranker
  co-resident).
- Batch size at query time: 1 (single user query).
- Device: GPU (`hatchet-worker-ai`) when present, CPU fallback otherwise.

Version pinning: `EMBED_MODEL_VERSION` env var = exact HF revision. Stored
on every passage row as `silver.document_passages.embedding_model_version`
so re-encoders can detect drift.

## 3. Sparse retriever (SPLADE++)

- Model: `naver/splade-cocondenser-ensembledistil`.
- Max features: ~30k vocab.
- Sparse vectors written to the Qdrant `text` slot alongside dense.
- Query time: same encoder; emits sparse query.
- Version pinning: `SPARSE_MODEL_VERSION` on every passage row.

## 4. Qdrant collections

> **Pass 4 update (ADR-0010, 2026-05-29):** the canonical chunked corpus
> is now `silver.document_passages` (was `silver.reports.sections_text`).
> A new Qdrant collection `georag_chunks` is the new primary feeder. The
> legacy `georag_reports` collection stays during the cutover; eventual
> deprecation. See [Ch 16 §3](../manual/16-algorithmic-spines.md).

Three collections during the transition:

### 4.1 `georag_reports`

Created in [src/dagster/georag_dagster/assets/index_reports.py:306-333](../../../src/dagster/georag_dagster/assets/index_reports.py).

```python
vectors_config = {
    "": VectorParams(size=384, distance=COSINE, on_disk=False),
}
sparse_vectors_config = {
    "text": SparseVectorParams(index=SparseIndexParams(on_disk=False)),
}
hnsw_config = HnswConfigDiff(m=32, ef_construct=256)
optimizers_config = OptimizersConfigDiff(
    indexing_threshold=5000,
    default_segment_number=2,
)
quantization_config = ScalarQuantization(
    scalar=ScalarQuantizationConfig(
        type=ScalarType.INT8,
        always_ram=True,
        quantile=0.99,
    ),
)
on_disk_payload = True
```

### 4.2 `georag_public_geoscience`

Mirror of the above; created in
[src/dagster/georag_dagster/assets/index_public_geoscience.py:201](../../../src/dagster/georag_dagster/assets/index_public_geoscience.py).

### 4.2b `georag_chunks` (Pass 4 — canonical chunked corpus)

[src/dagster/georag_dagster/assets/index_document_passages.py:64](../../../src/dagster/georag_dagster/assets/index_document_passages.py).
Same dense + sparse slot shape as `georag_reports`, but the source is
`silver.document_passages` (parent-child chunker) with `parent_chunk_id`
and `chunk_kind` payload fields. The cutover from `georag_reports` →
`georag_chunks` is gated on the backfill (script:
[\_backfill_document_passages_to_qdrant.py](../../../src/dagster/scripts/_backfill_document_passages_to_qdrant.py))
and the reranker-chain re-target ([test_reranker_uses_document_passages_canonical.py](../../../src/dagster/tests/test_reranker_uses_document_passages_canonical.py)
is the contract pin).

### 4.3 Payload schema

Every point carries:
```json
{
  "passage_id":         "uuid",
  "workspace_id":       "uuid",
  "project_id":         "uuid | null",
  "source_table":       "silver.document_passages | silver.lithology | …",
  "source_pk":          {"…": "…"},
  "document_id":        "uuid | null",
  "page_first":         "int | null",
  "page_last":          "int | null",
  "text":               "string",
  "section_path":       ["string", "…"],
  "category_codes":     ["string", "…"],
  "embedding_model_version": "string",
  "sparse_model_version":    "string",
  "ingested_at":        "iso8601"
}
```

### 4.4 Payload indexes

Created at collection-creation time via
`client.create_payload_index(field_name=…, field_schema=…)`:

| Field | Type | Why |
|---|---|---|
| `workspace_id` | keyword | Tenant pre-filter — applied to every search |
| `project_id` | keyword | Project-scoped retrieval |
| `source_table` | keyword | Constrain to passages / silver rows |
| `document_id` | keyword | Cite-back to source document |
| `category_codes` | keyword[] | Data-hierarchy facet filter |
| `page_first` | integer | Page-range filters |
| `page_last` | integer | Page-range filters |

## 5. BM25 / tsvector (Postgres lexical leg)

- Column: `silver.document_passages.text_tsv tsvector GENERATED ALWAYS AS
  to_tsvector('english', text) STORED`.
- Index: GIN on `text_tsv`.
- Weights: `setweight(to_tsvector(title), 'A') || setweight(to_tsvector(section_path),'B') || setweight(to_tsvector(text),'C')`.
- Query: `plainto_tsquery('english', query)` with rank `ts_rank_cd`.
- Tenant fence: `workspace_id = current_setting('app.workspace_id', true)::uuid`.

## 6. Fusion (RRF + DBSF)

Implemented in [src/fastapi/app/services/fusion.py](../../../src/fastapi/app/services/fusion.py).

### 6.1 RRF (Reciprocal Rank Fusion)

```
score(d) = sum over rankers r of: 1 / (k + rank_r(d))
```

Constants:
- `RRF_K = 60` (Cormack & Clarke default).
- Tie-break: prefer the higher individual-leg score.

### 6.2 DBSF (Distribution-Based Score Fusion)

```
norm_score(d, r) = (score_r(d) - mean_r) / std_r
score(d) = sum over rankers r of: norm_score(d, r)
```

Used when score distributions across legs are well-behaved (e.g., dense +
sparse + BM25 all in similar ranges). RRF is the default for the
agentic path; DBSF is selectable per intent.

### 6.3 Per-intent profile

| Intent | Method | Dense candidates | Sparse candidates | BM25 candidates | Fused cap | Rerank cap |
|---|---|---|---|---|---|---|
| `factual_lookup` | RRF | 40 | 40 | 40 | 30 | 10 |
| `synthesis` | RRF | 80 | 80 | 80 | 60 | 20 |
| `hypothesis_generation` | DBSF | 100 | 100 | 100 | 80 | 20 |
| `anomaly_detection` | (tool-driven, no fusion) | — | — | — | — | — |
| `uncertainty_quantification` | RRF | 60 | 60 | 60 | 40 | 15 |
| `decision_support` | DBSF | 80 | 80 | 80 | 60 | 20 |
| `project_summary` | RRF | 60 | 60 | 60 | 40 | 15 |
| `coverage_gap` | RRF | 40 | 40 | 40 | 30 | 10 |

Defined in [src/fastapi/app/agent/agentic_retrieval/retrieval_profile.py](../../../src/fastapi/app/agent/agentic_retrieval/retrieval_profile.py).

## 7. Reranker

- Model: `BAAI/bge-reranker-base`.
- Implementation: `sentence_transformers.CrossEncoder`.
- Device: CPU (in `fastapi` container).
- Thread budget: `OMP_NUM_THREADS=10`,
  `TOKENIZERS_PARALLELISM=false`
  ([docker-compose.yml:898-900](../../../docker-compose.yml)).
- Timeout: `asyncio.wait_for(..., timeout=8.0)`.
- Input cap per pair: query + passage truncated to 2000 chars each
  (per [notes/INDEX.md#project_latency_fix_2026_05_20](../notes/INDEX.md#project_latency_fix_2026_05_20)).
- Candidate cap: per the rerank cap column in §6.3.
- Fine-tune in flight via LoRA — see [notes/INDEX.md#project_reranker_v1](../notes/INDEX.md#project_reranker_v1).

## 8. Refusal thresholds

Defaults (overridable via `RetrievalProfile`):

| Signal | Threshold | Effect |
|---|---|---|
| `retrieval_quality_score` (max fused score) | < `RETRIEVAL_QUALITY_THRESHOLD` (default 0.6, [docker-compose.yml:992](../../../docker-compose.yml)) | Refuse with `refusal_reason='retrieval_quality_below_threshold'` |
| Reranker top score | < 0.45 | Refuse with `refusal_reason='reranker_low_top_score'` |
| Citation candidates after Layer 5 | 0 | Refuse with `refusal_reason='no_grounded_evidence'` |
| Numeric claim mismatch on Layer 3 | any | Demote the claim (strip from answer) |
| Geological constraint violation on Layer 6 | any | Demote the claim |

## 9. Citation binding

1. LLM emits text with `[ev:xxxxxxxx]` markers (8-hex prefix of an
   `silver.evidence_items.evidence_id`).
2. `agent/citation_binding.py` walks the answer:
   - Find every `[ev:…]` match.
   - Resolve each against the candidate evidence set (the fused-then-
     reranked list).
   - On miss: Layer 5 (provenance) fires → demote the surrounding sentence.
   - On hit: persist `silver.answer_citation_items` + a
     `silver.answer_citation_spans` row with `(start_char, end_char)`.
3. The Reverb `QueryCitation` event ([Appendix B §1.1](B-event-payloads.md))
   fires the moment the binding lands → inline pill appears.

## 10. Numerical claim verification (Layer 3)

[src/fastapi/app/agent/hallucination/layer3_numerical.py](../../../src/fastapi/app/agent/hallucination/layer3_numerical.py).

1. Claim extractor regex pulls "{value} {unit}" tuples from the answer
   (e.g., "2.3 g/t Au").
2. For each, find the cited evidence row's structured payload.
3. Compare with unit normalisation (Au g/t ↔ ppm, etc.).
4. Tolerance: numerical match within 5 % OR within the original source's
   reported uncertainty (whichever is larger).
5. Mismatch → demote the claim with `reason='layer3_numeric_mismatch'`.

## 11. Confidence score formula

Written to `silver.answer_runs.confidence` by
[agent/confidence_computer.py](../../../src/fastapi/app/agent/confidence_computer.py):

```
retrieval_quality = max(fused_score over kept candidates)
rerank_quality    = mean(rerank_score over kept candidates after rerank)
citation_density  = bound( citations / max(1, sentences), 0, 1 )
layer_pass_ratio  = layers_passed / 6     # six hallucination layers
cross_store_agree = 1 if cross-store check passed else 0.5

confidence = (
    0.30 * retrieval_quality +
    0.25 * rerank_quality +
    0.15 * citation_density +
    0.20 * layer_pass_ratio +
    0.10 * cross_store_agree
)
```

Surfaced to the frontend in the `QueryComplete` Reverb event.

## 12. Golden query eval suite

- Location: [src/fastapi/run_eval.py](../../../src/fastapi/run_eval.py),
  [src/fastapi/run_eval_120.py](../../../src/fastapi/run_eval_120.py).
- Suite: 120 curated queries per workspace template (gold, base metal,
  uranium SK).
- Pass criteria (per query):
  - Citation count ≥ 1.
  - All citations resolve to a real `evidence_items` row.
  - No claim demoted on Layer 3 / 6.
  - Top‐3 retrieval recall ≥ 0.80 vs. labelled corpus.
  - Reranker top score ≥ 0.50.
- Aggregate gate (for milestone acceptance):
  - **Citation coverage ≥ 95 %**.
  - **Refusal-on-bad-input ≥ 90 %** (the bad-input adversarial sub-set).
  - **Hallucination-block ratio ≥ 99 %** (claims that *should* have been
    blocked actually were).
- Nightly run: Hatchet `eval_real_rag_nightly` workflow; results into
  `eval.eval_runs` and `eval.eval_metrics_*` tables, plus a Grafana panel.

## 13. Workspace fence at every leg

- Dense: Qdrant filter `payload.workspace_id == $ws`.
- Sparse: same filter.
- BM25: `WHERE workspace_id = current_setting('app.workspace_id', true)::uuid`.
- Reranker: pairs come pre-filtered.
- Graph traversal: `MATCH (n {workspace_id: $ws})` everywhere.

A workspace_id mismatch on a retrieved candidate is an **invariant
violation** — emit a `cross_workspace_leak` audit row and refuse the
turn.

## 14. Per-store timeouts

From [docker-compose.yml:986-991](../../../docker-compose.yml):

| Store | Env | Default |
|---|---|---|
| PostGIS | `TIMEOUT_POSTGIS_S` | 5 s |
| Neo4j | `TIMEOUT_NEO4J_S` | 3 s |
| Qdrant | `TIMEOUT_QDRANT_S` | 2 s |
| Redis | `TIMEOUT_REDIS_MS` | 500 ms |
| Outer gather | `TIMEOUT_GATHER_S` | 180 s |
| Reranker | (hardcoded) | 8 s |

The parallel-gather retrieval node waits the lesser of (per-store
timeout, gather timeout) per leg, fuses the legs that returned in time,
and proceeds. A timed-out leg does NOT block the answer — but the
`silver.answer_runs.degraded_legs` column records which legs missed.

## 15. Replay determinism

Given:
- The same passage corpus (frozen Qdrant + Postgres snapshot).
- The same `embedding_model_version` + `sparse_model_version` +
  `reranker_version`.
- The same `RetrievalProfile`.
- The same query.

→ Replay produces the same ordered candidate list and the same
reranker scores. Used by SupportCockpit to reproduce an old turn against
the current model versions and surface drift.

## 16. Open items

- Persist `gold.significant_intersections` as a real table (Appendix A
  §4).
- Wire layer-1 retrieval-quality threshold to be per-workspace (not
  global).
- Add per-document-category retrieval profile selection (Ch 13).
- LoRA reranker training pipeline → re-bake `bge-reranker-base` weights
  with workspace-specific data (currently the in-place LoRA path).
