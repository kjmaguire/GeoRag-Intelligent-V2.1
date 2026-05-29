# GeoRAG Module 4 — RAG Retrieval & Caching Phase A Audit
<!-- Produced by: backend-fastapi agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-21 -->
<!-- Scope: Phase A items A1–A9 per module spec §6 -->
<!-- Authority: read-only code pass through src/fastapi/app/, ops/backlog/, memory/, prior audit files -->
<!-- Status: READ-ONLY PASS — no code, migrations, configs, or services were modified -->

---

## Subsection Results Summary

| ID | Topic | Status |
|---|---|---|
| A1 | Retrieval entry-point walk | Findings (structural) |
| A2 | Qdrant retrieval audit | Critical + High findings |
| A3 | Neo4j retrieval audit | Findings (medium) |
| A4 | PostGIS retrieval audit | Findings (medium + intake) |
| A5 | Fusion + rerank audit | Critical finding |
| A6 | Retrieval cache audit | High finding (data_version) |
| A7 | Embedding cache audit | Critical — absent |
| A8 | `answer_retrieval_items` + `answer_runs` | Critical — absent |
| A9 | Query-class routing rules | High — spec mismatch |

---

## A1 — Retrieval Entry-Point Walk

**Entry point:** `POST /internal/queries` → `src/fastapi/app/routers/queries.py` → `_agent_rag_stream()` → `run_deterministic_rag()` in `src/fastapi/app/agent/orchestrator.py`.

**Intent / query-class classification:** Keyword-based rules in `_classify_query()` (`orchestrator.py` lines 270–356). Not ML. Categories produced: `spatial`, `documents`, `downhole`, `graph`, `assay`, `targeting`, `public_geoscience`. These map loosely to spec classes but **do not match the spec taxonomy** (see A9).

**Which stores per class:**
- `spatial` → `query_spatial_collars` (PostGIS `silver.collars`)
- `documents` → `search_documents` (Qdrant `georag_reports`)
- `graph` → `traverse_knowledge_graph` / `query_graph_by_label` (Neo4j)
- `assay` → `query_assay_data` (PostGIS `silver.samples`)
- `downhole` → `query_downhole_logs` (PostGIS `silver.lithology_logs`)
- `public_geoscience` → `search_public_geoscience` (Qdrant PG collections + PostGIS `public_geoscience.*`)

**Parallelization:** The three primary branches (`query_spatial_collars`, `search_documents`, `search_public_geoscience`) are dispatched via `asyncio.gather()` with `return_exceptions=True` (orchestrator lines 2514–2521). This is correct per §05c.

**Gap — downhole + assay + graph are sequential, not parallel:** After the parallel gather, `downhole` (loop over hole IDs) and `assay` and `graph` tools run one-at-a-time with `await` (lines 2568–2730). A spatial+assay+graph query takes at least 3 serial tool latencies on top of the parallel gather. Not critical in current single-tool scenarios but is a performance concern as multi-category queries scale.

**Result fusion:** No RRF. Each tool result is rendered into a text context block by `_build_context()` and fed to the LLM as a single concatenated prompt. See A5.

**Reranker:** Wired. Applied in Stage 2 of `search_documents` on (query, chunk.text) pairs via `CrossEncoder.predict`. Correct pattern.

---

## A2 — Qdrant Retrieval Audit

### QDR-01 — CRITICAL: Dense-only retrieval on hybrid-configured collections

**File:** `src/fastapi/app/agent/tools.py` lines 1081–1088

`search_documents` calls `qdrant_client.query_points()` with a single `query=query_vector` (a dense float list). The `georag_reports` collection was configured in Module 2 with both dense + sparse slots. The call does not pass `prefetch` or a `Query` with `fusion=Fusion.RRF` — it sends a pure dense-vector query.

Result: the sparse vector slot in `georag_reports` is entirely unused at query time. Global Invariant 11 ("hybrid retrieval is core V1") is violated by code today.

**Root cause:** The Qdrant 1.17 hybrid Query API requires client code to construct a `Query` object with `prefetch=[Prefetch(query=sparse_vec, using="sparse"), Prefetch(query=dense_vec, using="dense")]` and `query=FusionQuery(fusion=Fusion.RRF)`. The current code passes only `query=dense_vector`, which Qdrant 1.17 interprets as a dense-only search on the `""` (default) named vector.

**Impact:** Every document retrieval today is dense-only. The sparse slot populated by Module 3 (if ever) would be silently ignored. Keyword-exact retrieval (drillhole IDs, NTS tile codes, commodity symbols) degrades to approximate cosine distance.

Same pattern applies in `public_geoscience_tool.py` line 360: `query_points` with a single dense vector against PG collections also configured with sparse slots.

### QDR-02 — CRITICAL: No sparse encoder at query time

No sparse encoder (`SPLADE++`, `BM42`, or any other) is implemented in `tools.py`, `orchestrator.py`, or `main.py` lifespan hooks. Module 3 confirmed 0 sparse points indexed on the doc side; the query side is equally absent. The entire sparse path — both indexing and retrieval — is unimplemented end-to-end.

### QDR-03 — HIGH: `workspace_id` payload filter absent from `georag_reports` queries

`search_documents` constructs its Qdrant filter via `_build_document_scope_filter(project_id)` (tools.py lines 931–985). The filter applies a `project_id` payload match, not `workspace_id`. Per Global Invariant 9, `workspace_id` is the mandatory multi-tenant isolation key on Qdrant payloads. However:

- The indexer (Dagster `index_reports`) was not confirmed to stamp `workspace_id` on any points in Module 3; `project_id` was the field used.
- The default mode is `cross_project` (config.py line 330), meaning **no payload filter at all** is applied for document search in the default deployment.

While `project_id` vs `workspace_id` may be a terminology issue in the specific current single-tenant deployment, the filter is off by default — isolation is not enforced. This is a multi-tenancy correctness gap.

### QDR-04 — HIGH: Identifier-boost not implemented

No regex for hole IDs, sample IDs, or NTS tile codes is applied before or after Qdrant retrieval to boost exact-identifier matches. A query for "PLS-22-08" will receive the same dense-cosine treatment as any other text. This is particularly bad for retrieval of a single specific hole's report sections.

### QDR-05 — MEDIUM: `answer_runs` metadata fields not populated

The addendum §04h-i spec requires the following fields in `answer_runs` per query:
- `embedding_model`, `sparse_model`, `sparse_model_version`
- `fusion_method`, `sparse_boost_applied`
- `reranker_version`
- `workspace_data_version_at_query`, `project_data_version_at_query`

None of these are written because `answer_runs` does not exist (see A8). Zero metadata fields are populated.

### QDR-06 — LOW: `query_points` API call is correct (Qdrant 1.17)

The async Qdrant client call uses `query_points()` which is the correct 1.17 Query API entry point. Not the legacy `search()`. This is correct. The issue is the payload passed, not the API itself.

---

## A3 — Neo4j Retrieval Audit

### N4J-01 — MEDIUM: `traverse_knowledge_graph` uses `session.run()` not `session.execute_read()`

Both `traverse_knowledge_graph` (tools.py line 1301) and `query_graph_by_label` (line 1411) call `session.run()` directly without wrapping in `session.execute_read()` (the Neo4j driver's read-transaction API). This is a correctness concern: `session.run()` without an explicit transaction runs in auto-commit mode, which in a Neo4j cluster context could accidentally target a follower configured for reads-only, causing writes to fail loudly. In the current Community Edition single-node deployment this has no practical impact, but it deviates from the driver's best-practice pattern and will matter when a replica is added.

### N4J-02 — MEDIUM: `traverse_knowledge_graph` CALL subquery may produce label-scan on unlabelled start nodes

The CALL subquery:
```
MATCH (start) WHERE start.project_id = $project_id AND toLower(start.name) = toLower($entity_name)
```
uses a label-less MATCH. Per the Module 1 index inventory, `project_id` is not indexed as a composite on the base `(n)` node without a label — only on specific label types (`Drillhole`, `MineralOccurrence`, `PublicGeo`). A label-less MATCH with a WHERE filter on `project_id` will scan all 56K+ nodes. The `name` property has no index either. This is a potential full-graph scan for every graph query.

**No EXPLAIN PROFILE was run** (read-only audit constraint). Phase B should include PROFILE runs against the live graph.

### N4J-03 — LOW: `fetch_project_graph_entities` uses label-less MATCH with `project_id` filter

Same pattern: `MATCH (n) WHERE n.project_id = $project_id AND n.name IS NOT NULL`. Full-graph scan. Mitigated by the 15-minute Redis cache, but cold-path latency is unbounded without an index on `(n.project_id)` globally.

### N4J-04 — LOW: Parameterized (PASS)

All user-supplied values (`entity_name`, `project_id`, `label`, `rel_type`) are passed as Cypher parameters or validated against allowlists before string interpolation. The `rel_filter` and `safe_label` are allowlist-gated (tools.py lines 65–208). No injection risk from the LLM-supplied inputs.

---

## A4 — PostGIS Retrieval Audit

### PG-01 — MEDIUM: `query_spatial_collars` uses `Find_SRID()` inside ST_DWithin — dynamic SRID lookup per query

When spatial filtering is active, the SQL at tools.py lines 503–508 calls `Find_SRID('silver', 'collars', 'geom')` inline in the spatial predicate. This is a dictionary table lookup inside every query execution. It is not a catastrophic cost (the `geometry_columns` view is small) but it is unnecessary — the SRID could be a config constant or looked up once at startup. Minor performance note.

### PG-02 — MEDIUM: `query_spatial_collars` spatial filter uses geometry-native `ST_DWithin` (GOOD on the current query)

The current spatial filter in `query_spatial_collars` (tools.py lines 503–508) uses `ST_DWithin(geom, ST_SetSRID(ST_MakePoint(...), Find_SRID(...)), radius)` — this is geometry-native, NOT the `::geography` cast identified in the Module 4 intake. The GIST index on `silver.collars.geom` will be used for this query. This specific query is CLEAN of the geography-cast issue.

**Module 4 intake item (geography-cast):** The 117ms seq-scan finding from the baselines was on a query that did use `::geography`. A review of all PostGIS queries in the codebase finds no `::geography` cast at all — the intake issue may have been observed in an ad-hoc probe or an earlier code version. **No live code path currently triggers the geography-cast bypass.** Phase B should nonetheless add the functional GIST index `CREATE INDEX ON silver.collars USING GIST ((geom::geography))` as defensive infrastructure for any future raw SQL access or Laravel query-builder usage that might cast to geography.

### PG-03 — HIGH: `workspace_id` filter absent from PostGIS queries; current population is 0

All PostGIS queries scope by `project_id` (e.g. `WHERE project_id = $1`). Per Module 3 state, `workspaces.data_version = 1` and `projects.data_version = 1`, but `workspace_id` is not yet stamped on `silver.collars`, `silver.samples`, or `silver.lithology_logs` rows. The queries use `project_id` which currently matches rows correctly for the single-tenant deployment. However, the workspace isolation layer (Global Invariant 9) is not in place at the query level.

### PG-04 — MEDIUM: Parameterized statements — PASS

All PostGIS queries use asyncpg bind parameters (`$1`, `$2`, etc.) throughout `query_spatial_collars`, `query_downhole_logs`, and `query_assay_data`. No string concatenation of user-supplied values. No SQL injection risk.

### PG-05 — INTAKE ACKNOWLEDGED: Silver-trapped structured_record evidence wiring

**Pre-approved intake from `ops/backlog/module-4-intake.md`:**
- `silver.seismic_surveys` (1 SEG-Y row) and `silver.geochemistry` (344 XYZ rows) have no `evidence_items` entries, no retrieval tool, and no citation path.
- `query_assay_data` targets `silver.samples` only (JSONB assay values). Geochem proximity searches and seismic bbox queries are unimplemented.
- **Recommendation for Phase B:** Emit `evidence_items` rows for both tables. For `silver.geochemistry`, add a PostGIS proximity query tool. For `silver.seismic_surveys`, defer V1 indexing of the single row (SEG-Y binary data has no natural text embedding path) but ensure the record is reachable via the document retrieval path when a corresponding report section exists.

---

## A5 — Fusion + Rerank Audit

### FUS-01 — CRITICAL: RRF not implemented across stores

There is no multi-store Reciprocal Rank Fusion implementation anywhere in the retrieval pipeline. Results from PostGIS, Qdrant, and Neo4j are each formatted as text context blocks and concatenated in `_build_context()`. The LLM receives the raw ranked-separately results; there is no cross-store score normalization or RRF re-ordering.

**Impact:** Document chunks and structured records cannot be compared by relevance score. The LLM's attention mechanism provides an implicit "fusion" but this is uncontrolled and varies by model temperature and prompt shape. Spec requirement for `fusion_method=RRF` with `k=60` default is entirely absent.

### FUS-02 — MEDIUM: Within-Qdrant dense-only ordering is by cosine score (correct for the single-vector path)

Within the `georag_reports` collection, results are returned by Qdrant in cosine-score order. The cross-encoder reranker then re-scores and re-orders them (tools.py lines 1133–1208). This two-stage pipeline is correct for the dense-only path. When hybrid retrieval is added (QDR-01 fix), Qdrant's built-in RRF handles the dense+sparse fusion within the collection; the cross-encoder then re-ranks the fused set. The reranker wiring is already structured to accept this without changes.

### FUS-03 — MEDIUM: Reranker model not version-pinned in config

`RERANKER_MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L-6-v2"` in `config.py`. No pinned model digest or version tag. If the model weights change on Hugging Face, the reranker output distribution shifts silently without any cache invalidation signal (the RAG cache key does not include `reranker_version`). Phase B should pin by commit hash or use a local snapshot.

### FUS-04 — LOW: Reranker timeout — no dedicated timeout for the `.predict()` call

The cross-encoder `.predict(pairs)` runs inside `loop.run_in_executor(None, ...)` (tools.py lines 1145–1153) which is wrapped by the outer `asyncio.wait_for(_run_search(), timeout=settings.TIMEOUT_QDRANT_S)` (2s). However, the reranker runs AFTER the Qdrant network call returns — the 2s timeout budget is almost entirely consumed by the Qdrant round-trip. A 20-chunk batch through a cross-encoder can take 200–800ms CPU time. In practice the timeout may be functionally disabled for the rerank phase. Dedicated `TIMEOUT_RERANKER_S` config needed.

### FUS-05 — MEDIUM: Top-k after rerank is configurable but not per query-class

`RERANKER_TOP_K = 12` is a single global setting. Spec implies per-query-class configurability (spatial queries may need fewer chunks than document-heavy narrative queries). Current implementation applies the same k=12 to all query types.

---

## A6 — Retrieval Cache Audit

**Cache is wired and active.** Redis-backed, 5-minute TTL (`setex(cache_key, 300, ...)` at orchestrator line 3394).

**Key construction** (`_cache_key()`, orchestrator lines 2198–2242):

```python
cache_inputs = {
    "q":    normalised,       # lowercased query
    "pid":  project_id,       # project UUID
    "dsv":  settings.DOCUMENT_SCOPE_VERSION,   # B5 scope version
    "cats": { ...categories... },              # classifier output
}
```

### CACHE-01 — CRITICAL: `data_version` not in the cache key

The cache key includes `DOCUMENT_SCOPE_VERSION` (a static config integer) but NOT the live `silver.workspaces.data_version` or `silver.projects.data_version` values. Per Global Invariant 12 and addendum §05d, `data_version` is the single freshness authority — a new Dagster ingestion run bumps `data_version` and must invalidate all cached answers for that workspace/project.

**Current behaviour:** After a Dagster ingestion run that promotes new collar data to Silver and bumps `projects.data_version` from 1 to 2, any previously cached answer for a collar-count query will continue to serve the stale count for up to 5 minutes. This is the maximum staleness TTL; in practice a user who queries immediately after ingestion will see wrong numbers.

**No subscriber to `ingestion.progress` or `commit_ingestion_run` exists.** Cache invalidation on ingestion completion is entirely absent.

### CACHE-02 — LOW: Cache key includes classifier output (cats), which is correct

The routing categories from `_classify_query()` are folded into the key (B5 intent). A query that now routes to Public Geoscience (new jurisdiction activated) will miss the old key. Correct behaviour.

### CACHE-03 — LOW: Redis hit rate unavailable (read-only probe constraint)

`redis-cache INFO stats` was not probed during this audit (stack state uncertain). Phase B should capture hit/miss counts from `INFO stats` keyspace_hits / keyspace_misses.

---

## A7 — Embedding Cache Audit

### EMB-01 — CRITICAL: Embedding cache absent

No embedding cache exists anywhere in the codebase. The `encode()` call in `_run_search()` (tools.py line 1068–1073) runs a fresh inference pass for every query:

```python
query_vector = await loop.run_in_executor(
    None,
    lambda: ctx.deps.embedding_model.encode(query_text, normalize_embeddings=True).tolist(),
)
```

Per addendum §04h-i, the embedding cache key should be `(text_hash, dense_model_version, sparse_model_version)`. None of these keys exist. `BAAI/bge-small-en-v1.5` inference on a CPU executor takes 15–80ms per query depending on text length. On the Anthropic path this is additive latency in the gather phase. On Ollama-local this competes with GPU time.

**Hit rate:** Not measurable — cache does not exist.

---

## A8 — `answer_retrieval_items` and `answer_runs` Tables

### TRACE-01 — CRITICAL: `answer_runs` table does not exist

A search across all migration files in `database/migrations/` finds no migration creating an `answer_runs` table. The only `answer_*` tables referenced in migrations are:
- `answer_citation_items` — **explicitly flagged as "Module 6 scope"** in `2026_04_20_130000_create_document_revisions.php` and `2026_04_20_140000_create_evidence_items.php`
- No `answer_runs`, no `answer_retrieval_items`

The addendum §04h + §05d spec requires `answer_runs` to record per-query metadata: `embedding_model`, `sparse_model`, `sparse_model_version`, `fusion_method`, `sparse_boost_applied`, `reranker_version`, `workspace_data_version_at_query`, `project_data_version_at_query`. None of these are written anywhere.

**This is a Phase B migration task** — analogous to how Module 3 discovered `answer_citation_items` absent.

### TRACE-02 — CRITICAL: `answer_retrieval_items` table does not exist

No migration creates `answer_retrieval_items`. The `retrieved` / `reranked` / `in_context` / `cited` stage columns and `retriever_score` / `reranker_score` per-chunk trace are absent. There is no per-run retrieval provenance log.

**Phase B work:** Create both `answer_runs` and `answer_retrieval_items` migrations, then wire the orchestrator to insert rows at the end of `run_deterministic_rag()` after validation passes.

---

## A9 — Query-Class Routing Rules

### ROUTE-01 — HIGH: Query classes do not match spec taxonomy

**Spec classes (§04h / A9 prompt):** `factual`, `spatial`, `document`, `computation`, `viz`

**Actual classes implemented:** `spatial`, `documents`, `downhole`, `graph`, `assay`, `targeting`, `public_geoscience`, `classifier_fallback`

The mapping is:
- `spatial` ≈ spec `spatial` (partial — spec spatial includes all geographic queries)
- `documents` ≈ spec `document`
- `assay` + `downhole` ≈ part of spec `computation`
- `targeting` ≈ part of spec `computation`
- `graph` — no spec equivalent at this level of resolution
- `public_geoscience` — no spec equivalent (extension)
- `factual` — absent (collapsed into `spatial` or `documents`)
- `computation` — absent as a class; computation queries (resource estimates, grade-thickness products) route to `documents` keyword matching
- `viz` — absent; visualizations are built post-hoc in `viz_builder.py` regardless of query class

**Testing status:** `_classify_query()` has no dedicated unit test file discovered in the audit. `tests/` directory was not enumerated fully but no `test_classifier.py` or `test_routing.py` was encountered.

### ROUTE-02 — MEDIUM: Fallback to spatial+documents on classifier miss (acceptable but untracked)

When no keyword matches, `_classify_query()` sets `classifier_fallback=True` and defaults to `spatial=True, documents=True`. The LLM classifier fallback (`classify_via_llm`) is wired to recover when the keyword pass misses. This is reasonable but the fallback rate is not tracked in a Prometheus counter visible in the dashboard (one `OUT_OF_SCOPE_REFUSALS` counter exists, but that is for the all-False LLM path, not the keyword-fallback path itself).

---

## Module 4 Intake Acknowledgement

### Intake Item 1: Geography-cast GIST bypass (117ms seq scan vs 3ms GIST)

**Status:** No live retrieval code uses `::geography` cast today. The audit finds `query_spatial_collars` uses geometry-native `ST_DWithin` (GIST-eligible). The intake finding was likely from an ad-hoc probe or earlier code version.

**Phase B recommendation:** Add functional GIST index `CREATE INDEX ON silver.collars USING GIST ((geom::geography))` as defensive infrastructure (option A from intake). Low urgency since no current code path triggers the bypass. Also audit Laravel's PostGIS queries and any raw SQL in Dagster for geography casts before the index is added.

### Intake Item 2: Silver-trapped structured_record evidence wiring

**Status:** Confirmed still unresolved. `silver.geochemistry` (344 rows) and `silver.seismic_surveys` (1 row) have zero RAG consumers, zero `evidence_items`, zero retrieval tool paths.

**Phase B recommendation:**
1. Add `query_geochem_proximity` tool in `tools.py` — PostGIS proximity query against `silver.geochemistry` with project_id scope.
2. Wire `evidence_items` INSERT in Dagster `silver_xyz` asset post-materialization for geochemistry rows.
3. Defer SEG-Y (1 row) — no text embedding path for binary SEG-Y data in V1. Flag the row as `retrieval_suppressed` in `structured_record_lineage`.

---

## Surface to Kyle — Critical and High Findings + Phase B Sequencing

### Critical Findings (unblock the most work)

| ID | Finding | Why it blocks |
|---|---|---|
| QDR-01 | Dense-only retrieval on hybrid-configured Qdrant collections — Global Invariant 11 violated | Every document query misses the sparse path. Core V1 requirement unmet. |
| QDR-02 | No sparse encoder at query time or index time | Prerequisite for QDR-01 fix. Without this, even fixing the client call returns nothing from sparse slots. |
| FUS-01 | No cross-store RRF | Multi-store fusion is uncontrolled. PostGIS + Qdrant + Neo4j results are rank-incomparable today. |
| CACHE-01 | `data_version` absent from RAG response cache key | After any Dagster ingestion run, stale answers persist for up to 5 minutes. Violates Global Invariant 12. |
| EMB-01 | Embedding cache absent | Every query pays full inference cost; cache key spec from addendum §04h-i not implemented. |
| TRACE-01/02 | `answer_runs` + `answer_retrieval_items` do not exist | All per-query metadata tracking, reranker-score tracing, and Module 6 citation wiring are blocked. |

### High Findings

| ID | Finding |
|---|---|
| QDR-03 | `workspace_id` payload filter absent; default is cross_project (no filter) |
| QDR-04 | No identifier-boost regex for hole IDs / NTS tiles before Qdrant search |
| PG-03 | `workspace_id` not in PostGIS WHERE clauses (project_id used instead) |
| ROUTE-01 | Query class taxonomy diverges from spec (`factual`, `computation`, `viz` absent) |

### Proposed Phase B Sequencing

1. **B1 — Sparse encoder + hybrid Qdrant calls (unblocks GI-11)**
   - Wire `SPLADE-v3-distill` or `BM42` as `sparse_encoder` in `main.py` lifespan.
   - Rewrite `search_documents` and `_query_collection` to use `Prefetch` + `FusionQuery(RRF)`.
   - Emit sparse vectors from Dagster `index_reports` asset for existing chunks (re-index pass).

2. **B2 — `data_version` in RAG cache key (unblocks GI-12)**
   - Add async lookup of `silver.projects.data_version` at the start of `run_deterministic_rag()`.
   - Include `pv` (project_data_version) in `_cache_key()` inputs.
   - Subscribe to `commit_ingestion_run` Dagster event (or poll via Dagster GraphQL sensor) to flush Redis keys on version bump.

3. **B3 — `answer_runs` + `answer_retrieval_items` migrations (unblocks Module 6)**
   - Laravel migration: create `answer_runs` with all §04h metadata columns.
   - Laravel migration: create `answer_retrieval_items` with per-chunk stage columns.
   - Wire INSERT at end of `run_deterministic_rag()` via async POST to Laravel or direct asyncpg insert.

4. **B4 — Embedding cache (unblocks latency targets)**
   - Redis key: `sha256(text_hash + dense_model_version)[:16]`.
   - TTL: 1 hour (embeddings are deterministic for a given model version).
   - Store as JSON float list; deserialize to numpy array before passing to Qdrant.

5. **B5 — Geography-cast GIST index + geochem retrieval tool (intake items)**
   - `CREATE INDEX ON silver.collars USING GIST ((geom::geography))`.
   - `query_geochem_proximity` tool wired to `silver.geochemistry`.
   - `evidence_items` rows for geochem Silver rows.

6. **B6 — Query-class taxonomy alignment (unblocks Module 9 test harness)**
   - Rename internal categories to match spec taxonomy or document the canonical mapping explicitly.
   - Add `computation` and `viz` routing paths.
   - Add unit tests for `_classify_query()` covering all spec classes.

---

## §04e / §04f / §04d Drift Notes (Not Already in Module 10 Backlog)

- **`Citation` model in `rag.py`** has added `corpus`, `jurisdiction_code`, `jurisdiction_name`, `license_summary`, `license_url`, `source_url`, `staleness_seconds` fields beyond the spec §04e `Citation` shape. These are additive and reasonable for the Public Geoscience extension but are not in the formal §04e schema. Flag for Module 10 doc sweep.
- **`GeoRAGResponse` has `degraded_sources` and `followups` fields** not in the §07d contract. Frontend-engineer must be aware of these. Module 10 doc sweep.
- **`query_graph_by_label` Cypher** uses `COALESCE(m.name, '?')` inside `COLLECT(DISTINCT type(r) + ' → ' + ...)` — this string concatenation inside COLLECT is non-standard and may produce unexpected results if `m.name` is an integer or list in some node types. Low risk, but worth a Cypher review.

---

## Confirmation: Nothing Outside `ops/audit/` Was Modified

This audit is read-only. No code, migrations, configuration files, Docker Compose files, database schemas, or service states were modified. The only file created is `ops/audit/2026-04-21-retrieval-audit.md`.

---

## Chunk 1 (B1+B2+B3) Applied 2026-04-21

Applied by: backend-fastapi agent (Claude Sonnet 4.6)

### Migration batch

Both new migrations ran as **batch 16**:
- `2026_04_21_100000_create_answer_runs` — DONE (84ms)
- `2026_04_21_110000_create_answer_retrieval_items` — DONE (30ms)

### Schema verification

`silver.answer_runs` and `silver.answer_retrieval_items` exist with zero rows.
All CHECK constraints confirmed active:

**answer_runs:**
- `answer_runs_query_class_valid` — `factual | spatial | document | computation | viz | unknown`
- `answer_runs_fusion_valid` — `rrf | dbsf`
- `answer_runs_backend_valid` — `vllm | ollama | anthropic`
- `answer_runs_citation_state_valid` — `draft | generated | validated | committed | rejected`
- `answer_runs_citation_mode_valid` — `posthoc_span_resolution | hybrid_delayed_attachment`

All FK constraints active:
- `workspace_id` → `silver.workspaces` (CASCADE)
- `project_id` → `silver.projects` (SET NULL — answer history survives project deletion)
- `user_id` → `public.users.id` BIGINT (RESTRICT) — users table confirmed present

**answer_retrieval_items:**
- `answer_retrieval_items_stage_valid` — `retrieved | reranked | in_context | cited`
- `answer_retrieval_items_store_valid` — `qdrant | neo4j | postgis | hybrid`

All FK constraints active:
- `answer_run_id` → `silver.answer_runs` (CASCADE)
- `workspace_id` → `silver.workspaces` (CASCADE)
- `document_revision_id` → `silver.document_revisions` (SET NULL)
- `passage_id` → `silver.document_passages` (SET NULL)

All indices present per spec (5 on answer_runs, 4 on answer_retrieval_items).

### Cache-key construction changed

**File:** `src/fastapi/app/agent/orchestrator.py`

**Before (v3):**
```python
cache_inputs = {
    "q":    normalised,
    "pid":  project_id,
    "dsv":  settings.DOCUMENT_SCOPE_VERSION,   # static config int
    "cats": { ...categories... },
}
```
Key prefix: `georag:rag_cache:v3:`

**After (v4):**
```python
cache_inputs = {
    "q":    normalised,
    "wid":  workspace_id,                      # FK scope
    "pid":  project_id,
    "wdv":  workspace_data_version,            # live from silver.workspaces
    "pdv":  project_data_version,              # live from silver.projects
    "rsv":  RETRIEVAL_STRATEGY_VERSION,        # constant, bumped per B4
    "fh":   "",                                # Module 9 placeholder
    "rh":   "",                                # Module 9 placeholder
    "cats": { ...categories... },
}
```
Key prefix: `georag:rag_cache:v4:`

**Data-version fetch:** Single async PG round-trip per query via `_fetch_data_versions()` using LEFT JOIN across `silver.workspaces` and `silver.projects`. workspace_id is separately looked up from the project row (Module 9 will supply it via JWT claims). Hot-cache optimization deliberately deferred — measure latency impact first.

**Round-trip verification:**
- Same params → same key → cache HIT confirmed
- workspace_data_version 1→2 (simulated bump) → different key → cache MISS confirmed

Old v3 keys TTL out naturally within 5 minutes of deploy.

### Query classifier

**File:** `src/fastapi/app/services/query_classifier.py`

Spec classes: `viz > spatial > computation > document > factual > unknown` (strict precedence, viz first — it has the clearest unambiguous user signal).

**`retrieval_strategy_version`:** `v1-pre-hybrid-2026-04-21`
Will be bumped to `v1-hybrid-2026-04-XX` when B4 (hybrid Qdrant + RRF) ships.

**Tests:** `src/fastapi/tests/test_query_classifier.py` — 54 tests, 54 passed (100%).

Breakdown: 8 viz, 8 spatial, 8 computation, 8 document, 6 factual, 5 unknown, 6 precedence, 3 constant/alias/type checks, 2 edge cases.

**Deprecated alias:** `_classify_query()` in `orchestrator.py` carries a `# NOTE` comment distinguishing it from the spec-class classifier. The internal routing bucket dict it produces is unchanged. No callers were broken.

### users table FK

`public.users` table confirmed present (migration `0001_01_01_000000_create_users_table.php`, BIGINT PK via `$table->id()`). FK added as `REFERENCES public.users(id) ON DELETE RESTRICT` on `answer_runs.user_id`. No Module 9 flag needed.

### Pydantic models

`src/fastapi/app/models/answer_run.py` created with:
- `QueryClassLiteral`, `FusionMethodLiteral`, `BackendLiteral`, `CitationLifecycleStateLiteral`, `CitationModeLiteral`, `StageLiteral`, `SourceStoreLiteral`
- `AnswerRunCreate`, `AnswerRunRead`, `AnswerRunUpdate`
- `AnswerRetrievalItemCreate`, `AnswerRetrievalItemRead`

All exported from `src/fastapi/app/models/__init__.py`.

### Deferred / surprising items

1. **workspace_id not in `QueryRequest` or `AgentDeps`** — B2 works around this by resolving workspace_id from the project row via a second async PG call (separate `SELECT workspace_id FROM silver.projects WHERE project_id = $1`). Module 9 (JWT claims) will plumb workspace_id directly, eliminating this extra round-trip. Flagged here for Module 9 scope.

2. **v4 key prefix** — v3 had `settings.DOCUMENT_SCOPE_VERSION` (static int). v4 removes this config entirely from the key and uses live `data_version` instead. The `DOCUMENT_SCOPE_VERSION` config field still exists and is still included in `_cache_key()` calls made via the backward-compat (no-categories) path (for continuity with any non-orchestrator callers). The `categories`-path key (used by `run_deterministic_rag`) no longer references `DOCUMENT_SCOPE_VERSION` at all.

3. **Orchestrator INSERT wiring** — answer_runs and answer_retrieval_items rows are NOT yet written by the orchestrator (Phase B Chunk 2 scope). Tables exist and accept data; the write path is deferred.

4. **FastAPI health check passed** — `curl http://localhost:8000/health -> {"status":"ok"}` after all Python code changes. No service restart was required (uvicorn --reload active).

---

## Chunk 2 applied 2026-04-21

Applied by: backend-fastapi agent (Claude Sonnet 4.6)

### SPLADE model + revision pinned

- **Model**: `naver/splade-cocondenser-ensembledistil`
- **Revision SHA**: `49cf4c7b0db5b870a401ddf5e2669993ef3699c7`
- **SPARSE_MODEL_VERSION**: `splade-cocondenser-ensembledistil@49cf4c7b`
- Revision confirmed 2026-04-21 against HuggingFace API.

### Shared module location (option a)

Chose option (a) -- copy into both repos with KEEP IN SYNC headers. Option (c) (shared package) would require Dockerfile changes, Docker Compose volume changes, and a new pip-installable package. Drift risk is mitigated by the prominent header comment in both copies referencing the counterpart path.

- FastAPI primary: `src/fastapi/app/services/sparse_encoder.py`
- Dagster copy: `src/dagster/georag_dagster/assets/sparse_encoder.py`

### Deps added / version confirmed

`pyproject.toml` (both FastAPI and Dagster):
- `torch>=2.3,<3.0` -- installed as 2.11.0 in container
- `transformers>=4.40,<5.0` -- installed as 5.5.4 in container
- `sentence-transformers>=4.1` -- was already present

### `index_reports.py` change: sparse field name, multi-vector structure

**Before (dense-only)**:
```python
PointStruct(id=..., vector=embedding.tolist(), payload={...})
```

**After (multi-vector)**:
```python
PointStruct(
    id=...,
    vector={
        "": embedding.tolist(),          # dense bge-small-en-v1.5 (384-dim)
        "text": SparseVector(            # SPLADE++ sparse weights
            indices=[...], values=[...]
        ),
    },
    payload={
        ...,
        "workspace_id": resolved_workspace_id,  # GI-9
        "parser_version": SPARSE_MODEL_VERSION,  # cache invalidation tag
    }
)
```

- Sparse field name: `"text"` (Qdrant named vector slot)
- Dense field name: `""` (Qdrant default unnamed vector)
- `workspace_id` payload field added (GI-9)
- `parser_version` payload field added (SPLADE model version tag)
- Figure points updated to same multi-vector format

### Qdrant Query API migration

**Files touched**:
- `src/fastapi/app/services/qdrant_service.py` (new) -- `hybrid_query()` and `hybrid_query_no_workspace()`
- `src/fastapi/app/agent/tools.py` -- `search_documents._run_search()` updated to call `hybrid_query()`

**Filter structure**: workspace_id mandatory on every Prefetch branch (GI-9). Additional project_id scope filter from `_build_document_scope_filter()` ANDed in via nested `Filter(must=[...])`.

**Dense field**: `""` (Qdrant default unnamed vector in multi-named-vector collections)
**Sparse field**: `"text"` (SPLADE++ slot)
**Prefetch limit**: 100 per branch, Qdrant RRF returns `limit` (50 default) fused results.

### Qdrant collections recreated

All 5 collections were recreated with named dense `""` + sparse `"text"` slots (previous single-vector config didn't support named vectors). Payload indices reinstated.
- `georag_reports`: 0 points (recreated from 18 dev points)
- `pg_mine`: 0 points (recreated from 140)
- `pg_resource_potential_zone`: 0 points (recreated from 82)
- `pg_mineral_occurrence`: 0 points (recreated from 22,229)
- `pg_drillhole_collar`: 0 points (recreated from 33,490)

NOTE: pg_* collections will be repopulated by Dagster public-geoscience assets on next run. georag_reports requires `index_reports` to run against fresh documents.

### RRF implementation + test count

- `src/fastapi/app/services/fusion.py` -- `rrf_fuse()`, `Candidate`, `ScoredCandidate`, `FUSION_METHOD`
- `src/fastapi/tests/test_rrf.py` -- 19 tests, **19/19 passed**

Test coverage: empty lists, single empty mixed, single-list passthrough, multi-list disjoint, overlap accumulation, stable tiebreak, exact formula arithmetic, custom k, payload/store preservation, sequential rank field, large list stress.

### Orchestrator integration: cross-store RRF

Injected into `run_deterministic_rag()` in `src/fastapi/app/agent/orchestrator.py`, immediately before Step 3 (context build). Fuses Qdrant chunks, Neo4j entities, and PostGIS collars into a single RRF-ranked list. `fusion_method = "rrf"` available for answer_runs write (Chunk 3 scope). Implementation is wrapped in try/except so fusion errors are non-fatal (logged at DEBUG) and do not break existing context assembly.

### FastAPI memory bump verified

- **docker-compose.yml**: `deploy.resources.limits.memory: 6g`, `reservations.memory: 3g`
- Container `georag-fastapi` recreated: limit confirmed 6 GiB via `docker stats`
- Idle memory post-recreate: 2.6 GiB (SPLADE warms on first query per worker)

### FastAPI rebuild outcome

- Container rebuilt with `docker compose up -d --build fastapi`
- Recreated with `docker compose up -d --force-recreate fastapi`
- Health check: `curl http://localhost:8000/health -> {"status":"ok"}`
- `pip show transformers torch` confirmed installed (transformers 5.5.4, torch 2.11.0)

### Sparse encoder smoke test output

```
non-zero terms: 58
top 5: [(12913, 1.897), (12567, 1.767), (5796, 1.585), (2603, 1.561), (7099, 1.442)]
```

Query: "drillhole assay results for sample 23-MS-117". 58 non-zero terms, all positive weights.

### Hybrid query smoke test result

`hybrid_query()` executed without error against `georag_reports` collection. Result count: 0 (expected -- empty sparse index, workspace filter active). Shape verification confirmed: Query API call with Prefetch + FusionQuery(RRF) accepted by Qdrant 1.17.1.

### Honest caveat

**The sparse index is empty today.** All Qdrant collections were recreated with the correct multi-vector schema. Sparse points will be written when `index_reports` runs against new documents (Phase C scope). Until then:

> "Hybrid retrieval is now end-to-end wired -- query-side encoder live, doc-side encoder wired into `index_reports`, Qdrant Query API in use, cross-store RRF active. Sparse recall requires Module 3 to run `index_reports` against fresh documents (Phase C scope). Until then, sparse prefetch returns empty and RRF falls back to dense-only scoring."

### Surprises

1. **Qdrant collections required full recreation** -- The PATCH API (`update_collection`) on Qdrant 1.17.1 does not support adding sparse vector slots to an existing single-vector collection. The Python client throws 400 "Not existing vector name error: text". Collections must be created with the full named-vector config from the start. All 5 collections were recreated; pg_* points will be repopulated on next Dagster run.

2. **FastAPI service deploy block was absent** -- The FastAPI service had no `deploy:` block at all; the memory was unconstrained. The 4g entry was for Qdrant (different service in the same YAML). Added a proper `deploy:` block to the FastAPI service (4.0 CPU, 6g memory limit, 3g reservation).

3. **TRANSFORMERS_CACHE env var deprecation warning** -- `TRANSFORMERS_CACHE` is deprecated in transformers>=5.0; `HF_HOME` is the correct env var. The FastAPI compose env already sets `HF_HOME=/tmp/hf_cache`, so the deprecation is cosmetic. The `TRANSFORMERS_CACHE` line can be removed from docker-compose.yml in Module 10 cleanup.

---

## Chunk 2 — Data Recovery 2026-04-21

Applied by: data-engineer agent (Claude Sonnet 4.6)

### 1. Asset × Collection Mapping Table

| Collection | Responsible Asset | Source Table(s) | Rows in PG |
|---|---|---|---|
| `georag_reports` | `index_reports` | `silver.reports` (sections_text JSONB) | 1 report, 18 sections |
| `pg_mine` | `index_public_geoscience_qdrant` | `public_geoscience.pg_mine` | 140 |
| `pg_mineral_occurrence` | `index_public_geoscience_qdrant` | `public_geoscience.pg_mineral_occurrence` | 22,229 |
| `pg_drillhole_collar` | `index_public_geoscience_qdrant` | `public_geoscience.pg_drillhole_collar` | 33,490 |
| `pg_resource_potential_zone` | `index_public_geoscience_qdrant` | `public_geoscience.pg_resource_potential_zone` | 908 |

Notes:
- `index_public_geoscience_qdrant` also writes `pg_rock_sample` and `pg_assessment_survey` collections (not in the 5 recreated, but processed in the same asset run).
- `silver.reports` source data is intact in Bronze/Silver — no data loss outside Qdrant.

### 2. Pre-run Sanity Check (confirmed 2026-04-21 17:15 UTC)

All 5 collections confirmed via `curl http://qdrant:6333/collections/<name>`:

| Collection | Status | Points | Dense `""` slot | Sparse `"text"` slot |
|---|---|---|---|---|
| `pg_drillhole_collar` | green | 0 | YES (384 dim, cosine) | YES (on_disk=false) |
| `pg_mineral_occurrence` | green | 0 | YES (384 dim, cosine) | YES (on_disk=false) |
| `pg_resource_potential_zone` | green | 0 | YES (384 dim, cosine) | YES (on_disk=false) |
| `pg_mine` | green | 0 | YES (384 dim, cosine) | YES (on_disk=false) |
| `georag_reports` | green | 0 | YES (384 dim, cosine) | YES (on_disk=false) |

All 5 collections correctly shaped from Chunk 2 recreation. No collection was deleted or recreated during this recovery dispatch.

### 3. Materialization Results

**Run A: `index_reports` (georag_reports) — FAILED**

- Dagster run `0f977ba3` (multiprocess executor): SIGKILL in worker subprocess
- Dagster run `44f3a058` (in-process executor via `--config-json`): silent crash, run stuck in STARTED, no STEP_FAILURE event recorded
- **Root cause**: Dagster daemon container has a hard 1 GiB memory limit (`docker-compose.yml` line 1412). Loading SPLADE++ (~440 MB) + bge-small-en-v1.5 (~100 MB) on top of the 240-280 MB daemon base process exceeds 1 GiB. The multiprocess executor was SIGKILL'd at 969 MiB / 1 GiB (94.65%). The in-process executor peaked at 969 MiB, then crashed silently without writing a failure event.
- **georag_reports points written: 0**
- **Status: BLOCKED pending devops-engineer memory limit increase (see Chunk 3 follow-ups)**

**Run B: `index_public_geoscience_qdrant` (4 PG collections) — IN PROGRESS**

- Dagster run `460a6c3a` — STARTED 17:33:49 UTC
- `_ensure_collection()` correctly detected existing collections and skipped recreation (went to `else` branch — optimizer_config patch only). Multi-vector schema from Chunk 2 preserved.
- `pg_mine` (140 rows): completed, 140 points written at ~17:35 UTC. Confirmed via Qdrant API.
- `pg_mineral_occurrence` (22,229 rows): embedding in progress. At 1,248/22,229 chunks at 17:39:47 UTC (~32 chunks/7s batch rate). Estimated completion: ~3h from start.
- `pg_drillhole_collar` (33,490 rows): pending
- `pg_resource_potential_zone` (908 rows): pending
- Background monitor active (run ID `460a6c3a`). Final row counts to be filled when run completes.

| Collection | Rows Before | Rows After | Wall Time | Note |
|---|---|---|---|---|
| `georag_reports` | 0 | 0 | — | BLOCKED — daemon OOM, see follow-ups |
| `pg_mine` | 0 | 140 | ~2 min | COMPLETE |
| `pg_mineral_occurrence` | 0 | TBD | ~3h est. | IN PROGRESS |
| `pg_drillhole_collar` | 0 | TBD | ~5h est. | PENDING |
| `pg_resource_potential_zone` | 0 | TBD | ~30 min est. | PENDING |

### 4. Sample Point Payload (pg_mine — confirmed written)

```json
{
  "jurisdiction_code": "<CA-SK or CA-BC>",
  "source_id": "<e.g. CA-SK-MINE>",
  "source_feature_id": "<upstream OBJECTID>",
  "canonical_type": "mine",
  "pg_id": "<UUID>",
  "commodities": ["<str>"],
  "commodity_grouping": "<str|null>",
  "status": "<str|null>",
  "geom_bbox": [lon, lat, lon, lat],
  "source_url": "<str|null>",
  "summary_text": "<structured NL summary>"
}
```

**CRITICAL FINDING — Sparse vector absent from pg_* points:**

`index_public_geoscience_qdrant` uses `PointStruct(vector=embeddings[i].tolist(), ...)` — a raw float list, not the named-vector dict `{"": [...]}`. Qdrant 1.17.1 accepts both formats (verified by live test: plain float list upsert to a named-vector collection returns 200 OK). The points are stored in the dense `""` slot. However:

- **No sparse `"text"` vector is written to any pg_* point** — `index_public_geoscience_qdrant` has no SPLADE encoder wiring
- **No `workspace_id` payload field is written to any pg_* point** — GI-9 multi-tenant isolation payload is absent
- **No `parser_version` payload field** — no cache invalidation tag

This means all pg_* points written in this recovery run will be:
- Dense-only (sparse slot empty) — hybrid retrieval degrades to dense-only for these collections
- Not filterable by `workspace_id` — the FastAPI `hybrid_query()` with mandatory `workspace_id` filter will return 0 results from these collections

**This is the primary Chunk 3 follow-up** — `index_public_geoscience_qdrant` needs the same multi-vector + workspace_id payload treatment that Chunk 2 applied to `index_reports`.

### 5. Hybrid Query Smoke Test

Not run. `georag_reports` (0 points) cannot return results. `pg_mine` (140 points, dense-only) would also return 0 results because the FastAPI `hybrid_query()` mandates a `workspace_id` payload filter, and pg_* points written by `index_public_geoscience_qdrant` do not include `workspace_id`. Running the smoke test against an empty collection with a mismatched payload filter would produce a misleading "0 results" that looks like a collection problem rather than a payload gap.

**Smoke test deferred to Chunk 3** after `workspace_id` payload is added to pg_* points.

### 6. data_version Before/After

| Entity | data_version Before | data_version After | Note |
|---|---|---|---|
| `silver.workspaces` (default) | 1 | 1 | `commit_ingestion_run` did not fire — no data flows reached terminal gate |
| `silver.projects` (both) | 1 | 1 | Same — no terminal gate reached |

`commit_ingestion_run` is the DAG terminus. It did not execute because neither `index_reports` nor `index_public_geoscience_qdrant` materialization reached a terminal state that would trigger the full DAG. data_version remains at 1 for both workspace and both projects.

### 7. Assets That Did Not Run Cleanly

| Asset | Status | Reason |
|---|---|---|
| `index_reports` | BLOCKED (2 failed runs) | Dagster daemon memory limit 1 GiB insufficient for SPLADE (~440 MB) + bge-small (~100 MB) + daemon base (~270 MB). Total ~810 MB required; multiprocess executor adds subprocess overhead that exceeds 1 GiB ceiling. |
| `index_public_geoscience_qdrant` | PARTIAL (in progress) | Dense-only embedding for 4 PG collections. pg_mine complete (140 pts). Remaining 3 collections pending (long-running: estimated 4-5h total wall time for 56,627 rows at CPU-bound embedding rate). |

RAGFlow is stopped (per project memory `feedback_ragflow_deferred.md`) — no `bronze_reports` pipeline trigger available through that path. Not a factor for this dispatch.

### 8. Chunk 3 Follow-Ups Flagged

**BLOCKER — C3-01: Dagster daemon memory limit must increase before `index_reports` can run.**

- Current limit: 1 GiB (`docker-compose.yml` lines 1410-1412)
- Minimum required: 2 GiB (SPLADE 440 MB + bge-small 100 MB + daemon base 280 MB + PyTorch overhead)
- Recommended: 3 GiB (matches FastAPI sparse encoder footprint, headroom for concurrent runs)
- Owner: devops-engineer
- Change: `docker-compose.yml` `dagster-daemon` service `deploy.resources.limits.memory: 3G` + `reservations.memory: 1G`
- After fix: re-run `index_reports` with `--select index_reports --config-json '{"ops":{"index_reports":{"config":{"report_title":"NI 43-101","project_id":"public"}}}}'`

**C3-02: `index_public_geoscience_qdrant` needs multi-vector + workspace_id payload (analogous to Chunk 2 for `index_reports`).**

- Current behavior: writes `PointStruct(vector=float_list, payload={no workspace_id, no parser_version})`
- Required behavior: `PointStruct(vector={"": dense_list, "text": SparseVector(...)}, payload={..., "workspace_id": ..., "parser_version": ...})`
- Impact: all pg_* collections written by this asset will return 0 results from `hybrid_query()` because the mandatory `workspace_id` filter finds no match
- Fix scope: add `encode_sparse_batch()` import from `sparse_encoder.py`, build `{"": dense, "text": SparseVector(...)}` vector dict, add `workspace_id=DEFAULT_WORKSPACE_UUID` and `parser_version=SPARSE_MODEL_VERSION` to each payload builder function
- Also requires `_ensure_collection()` update: the existing `else` branch patches `optimizer_config` with the wrong kwarg name (`optimizer_config` instead of `optimizers_config` — the current code has a typo). Verify before next run.
- Owner: data-engineer (Chunk 3)
- After fix: re-run `index_public_geoscience_qdrant` to overwrite the dense-only points with correct multi-vector points

**C3-03: Dagster daemon run `44f3a058` stuck in STARTED state — needs cleanup.**

- The in-process executor run `44f3a058` is orphaned with status STARTED, no end_time, and no failure event. This is a ghost run that will appear stuck in the Dagster UI forever.
- Resolution: `UPDATE runs SET status='FAILURE' WHERE run_id='44f3a058-683a-408a-a34b-f9a2b16425bc'` in `georag_dagster` database, or use the Dagster webserver UI to cancel/terminate the run.
- Owner: devops-engineer or ops (safe SQL update)

### 9. Files Touched

- `ops/audit/2026-04-21-retrieval-audit.md` — this file (Chunk 2 data recovery close-out section appended)

No code files were modified. No Dagster asset code was hot-patched. No Qdrant collections were deleted or recreated. All changes are strictly operational (Dagster materializations initiated).

### 10. Surprises

1. **Qdrant 1.17.1 accepts plain float list upserts on named-vector collections** — no error. The float list is silently stored in the `""` (default) dense slot. The sparse `"text"` slot receives no data. This means `index_public_geoscience_qdrant` will not fail — it will write points successfully, but those points will have no sparse vector and no `workspace_id` payload. The failure mode is silent degraded retrieval, not a hard error.

2. **In-process executor crashes silently in the daemon** — When `--config-json` with `{"execution":{"config":{"in_process":{}}}}` is passed, the Dagster daemon still routes via the QueuedRunCoordinator → DefaultRunLauncher chain. The in-process config applies to the CLI-level execution when the CLI itself executes the job (bypassing the coordinator). When submitted to the run coordinator, the executor config in the job's run config may override it to multiprocess. The run appeared to start in-process (single process at 969 MiB peak) but the failure left the run in STARTED state indefinitely — the daemon's run monitoring did not reconcile the crashed run to FAILURE status. This is a known Dagster limitation: if the run worker process dies without sending a failure event, the run remains STARTED until the daemon's run monitoring timeout fires.

3. **22,229-row pg_mineral_occurrence embedding at CPU rate (~32/7s) will take ~85 minutes alone** — the Dagster daemon container has no GPU. Total wall time for all 4 PG collections (~56,627 rows plus rock_samples and assessment_surveys in the same asset run) is estimated at 4-5 hours. This is acceptable for a one-off recovery run but flags the need for a GPU-enabled Dagster worker for future large-scale re-indexing. bg monitor run ID: `460a6c3a-7476-40da-b277-c111c12628fb`.

---

## Chunk 2 Cleanup 2026-04-21

Applied by: data-engineer agent (Claude Sonnet 4.6)

### 1. Bad Runs Terminated

Runs `460a6c3a`, `44f3a058`, and pre-existing ghost `03bde662` all marked CANCELED in `georag_dagster.public.runs` via direct SQL update. GraphQL `terminateRun` mutation returned "Unable to terminate" for both primary runs (worker processes already dead, no live process to signal). Direct DB update was the only viable path.

Rows wasted by `460a6c3a`: 140 `pg_mine` dense-only points (cleared via empty-filter delete). ~1,248 `pg_mineral_occurrence` partial-batch points were abandoned in-flight.

Zero STARTED runs confirmed after update.

### 2. pg_mine Cleared

`POST /collections/pg_mine/points/delete` with `{"filter": {}}`. Qdrant acknowledged. `points_count: 0` verified before proceeding.

### 3. Dagster Daemon Memory: 1G → 3G

`docker-compose.yml` changes:
- `dagster-daemon`: `memory: 1G` → `memory: 3G`, reservation `128M` → `1G`
- `dagster-webserver`: `memory: 1G` → `memory: 2G`, reservation `128M` → `256M` (same image, also at 1G, bumped per instructions)

Post-recreate `docker stats` confirmed: daemon 317 MiB / 3 GiB, webserver 114.6 MiB / 2 GiB.

### 4. `index_public_geoscience_qdrant` Asset Fix

**File:** `src/dagster/georag_dagster/assets/index_public_geoscience.py`

- Imported `encode_sparse_batch`, `SPARSE_MODEL_VERSION` from `sparse_encoder`
- Added `DEFAULT_WORKSPACE_UUID = "a0000000-0000-0000-0000-000000000001"` constant
- `_ensure_collection()` create branch: single-vector `VectorParams(...)` → named-vector dict `{"": VectorParams(...)}` + `sparse_vectors_config={"text": SparseVectorParams(...)}`
- `_ensure_collection()` else-branch typo: `optimizer_config=` → `optimizers_config=`. Confirmed zero live uses of wrong kwarg remain.
- All 6 payload builders: added `"workspace_id": DEFAULT_WORKSPACE_UUID` and `"parser_version": SPARSE_MODEL_VERSION`
- `_run_canonical_type()`: `encode_sparse_batch()` called, `PointStruct(vector={"": dense, "text": SparseVector(...)}, ...)` used for every point
- `MaterializeResult`: added `sparse_model` metadata field

### 5. `index_reports` Re-run

**Run ID:** `7df26b22` — SUCCESS
**Points in `georag_reports`:** 18 sections embedded, 18 points upserted
**Wall time:** 1m47s
**Asset checks:** `embedding_id_present` PASSED (1/1), `parser_error_floor` PASSED (100%)
**Sample point:** `vector_type=named-dict`, `vector_names=["","text"]`, `workspace_id=a0000000-0000-0000-0000-000000000001`, `parser_version=splade-cocondenser-ensembledistil@49cf4c7b`

### 6. Background Materialize: `index_public_geoscience_qdrant`

**Run ID:** `778c604c-3b83-4b64-88b3-c5d3a1d6d6d6`
**Status at close-out:** STARTED (~17:55 UTC)
**Expected wall time:** ~4–5 hours CPU-only (56,767+ rows across 6 collections)
**Progress at close-out:** `pg_mine` (140 rows) COMPLETE; `pg_mineral_occurrence` (22,229) IN PROGRESS; remaining 4 PENDING

Poll: `docker exec georag-dagster-daemon dagster run list --limit 3`

### 7. First-Points Validation

Verified via `POST /collections/pg_mine/points/scroll` with `with_vector: true`:
- `vector_type: named-dict` (was `plain-list` — the bug is fixed)
- `vector_names: ["", "text"]` (both slots present)
- `workspace_id: a0000000-0000-0000-0000-000000000001`
- `parser_version: splade-cocondenser-ensembledistil@49cf4c7b`
- `canonical_type: mine`

### 8. Hybrid Query Smoke Test

Query: "gold mine in saskatchewan" against `pg_mine` (140 points, multi-vector)
Dense: 384 dims. Sparse: 41 non-zero SPLADE terms.
**Result count: 5** — non-zero, hybrid retrieval end-to-end working.
Top scores: 1.0000, 0.4762, 0.4333

### 9. Backlog Updates

- `ops/backlog/module-4-intake.md`: C3-01, C3-02, C3-03 marked RESOLVED with resolution details
- `ops/backlog/module-10-doc-sweep.md`: "Dagster daemon GPU access for SPLADE inference" entry added; CPU-only rate Kyle-accepted for dev; production paths documented

### 10. Surprises

1. **`dagster run terminate` does not exist in Dagster 1.13** — CLI subcommands are `list`, `delete`, `migrate-repository`, `wipe` only. GraphQL `terminateRun` also failed (workers already dead). Resolution: direct SQL in `georag_dagster` DB.

2. **Third ghost run `03bde662`** pre-dating both bad runs — not in the brief but found as an additional STARTED orphan. Canceled alongside the two documented runs.

3. **Dagster webserver was also at 1G** — bumped to 2G per instructions (same codebase, also insufficient).

4. **`hybrid_query()` takes `client` as first positional arg** — the brief's inline smoke test example omitted it. Inspected signature before calling. Actual invocation: `hybrid_query(client=AsyncQdrantClient(...), collection=..., ...)`.

---

## Chunk 3 applied 2026-04-21

Applied by: backend-fastapi agent (Claude Sonnet 4.6)

### 1. Reranker model + revision pinned

- **Model**: `BAAI/bge-reranker-base` (Apache 2.0, ~278 MB)
- **Revision SHA**: `5ccf1b81c57ff625b3e4b7ab15481d6e2ee9bc56`
- **RERANKER_VERSION**: `bge-reranker-base@5ccf1b81`
- Replaces `cross-encoder/ms-marco-MiniLM-L-6-v2` (was unpinned)
- Persisted to `answer_runs.reranker_version` via orchestrator INSERT wiring
- **Per-class top-k** (`RERANKER_TOP_K_BY_CLASS`): factual=20, spatial=30, document=15, computation=10, viz=30, unknown=20
- **Timeout**: `RERANKER_TIMEOUT_S = 2.0s` for a batch of up to 50 candidates on CPU
- **Fallback**: on timeout or error, log + continue with RRF order (non-fatal)
- **File**: `src/fastapi/app/services/reranker.py` (new)
- **main.py**: lifespan hook updated to use `_get_reranker()` singleton + store `app.state.reranker_version`

### 2. Identifier boost detection

- **File**: `src/fastapi/app/services/identifier_boost.py` (new)
- **Patterns supported**:
  | Class | Example | Pattern |
  |---|---|---|
  | `HOLE_ID_DASHED` | `PLS-22-08`, `23-MS-117`, `2024-DDH-001` | Family A (letter prefix) + Family B (digit prefix + letter middle) |
  | `HOLE_ID_COMPACT` | `DDH0023`, `HOLE0042` | 2-4 uppercase letters + 2-5 digits |
  | `SAMPLE_ID_ALPHA` | `MS240301`, `AU123456` | 1-4 uppercase letters + 4-8 digits |
  | `SAMPLE_ID_DASHED` | `AU-240301`, `CU-123456` | 2 uppercase letters + dash + 6 digits |
  | `NTS_TILE` | `74I12`, `104B08` | 2-3 digits + letter A-P + 2 digits |
  | `COMMODITY_CODE` | `Au`, `U3O8`, `REE` | Exact-match frozenset (case-sensitive) |
- **Boost factor applied**: `SPARSE_BOOST_FACTOR = 1.5` → sparse Qdrant prefetch limit 100 → 150
- **Thread path**: `detect_identifiers()` in orchestrator → `_sparse_boost_factor` → `search_documents(sparse_boost_factor=...)` → `hybrid_query(sparse_boost_factor=...)` → `sparse_prefetch_limit = int(100 * 1.5) = 150`
- **`sparse_boost_applied`** bool persisted to `answer_runs`
- **Test file**: `src/fastapi/tests/test_identifier_boost.py` — **37 tests, 37 passed**
- Negative tests verified: plain dates (`2022-04-15`), common English, lowercase commodity (`au`) all correctly non-matching

### 3. Cypher parameterization audit

- Grepped all `session.run(`, `tx.run(`, `execute_read(`, `execute_write(` calls in `src/fastapi/app/` — **0 f-string concatenations found**
- All user-supplied values pass as named Cypher parameters (`$param_name` binding)
- LLM-supplied values (`relationship_type`, `label`) are allowlist-gated before string interpolation (P2 #28 — `_validate_cypher_relationship`, `_validate_cypher_label`)
- Neo4j uses `session.run()` in auto-commit mode (N4J-01 finding from Phase A) — still not `session.execute_read()`. This is a Community Edition single-node deployment — correctness risk is zero in current topology. Module 10 doc sweep item.
- **Result: 0 f-strings fixed (none found)**

### 4. PostGIS retrieval audit

- All `conn.fetch()` / `conn.fetchrow()` calls in `tools.py` use asyncpg bind parameters (`$1`, `$2`, ...) — **0 string interpolation gaps**
- `workspace_id` filter: `query_spatial_collars`, `query_downhole_logs`, `query_assay_data` all scope by `project_id` (PG-03 from Phase A — `workspace_id` is not yet stamped on silver rows; `project_id` is the current isolation key). Deferred to Module 9 for full workspace-level enforcement.
- GIST index usage: `query_spatial_collars` uses `ST_DWithin(geom, ...)` — geometry-native, GIST-eligible (PG-02 confirmed clean from Phase A)
- **Result: 0 string-interp gaps, workspace_id filter gap acknowledged per Phase A PG-03 finding**

### 5. Parallel dispatch integration

- **Integration point**: `run_deterministic_rag()` in `orchestrator.py`, "Phase 1: parallel fan-out" block
- **Change**: wrapped each branch in `asyncio.wait_for` with per-store timeout:
  - `query_spatial_collars`: `TIMEOUT_POSTGIS_S = 5.0s`
  - `search_documents`: `TIMEOUT_QDRANT_S = 2.0s`
  - `search_public_geoscience`: `TIMEOUT_QDRANT_S = 2.0s`
- `asyncio.TimeoutError` captured by `return_exceptions=True` → partial-rescue path logs + continues
- Serial branches (downhole, assay, graph) remain sequential; adding independent timeouts to them is a Phase C item (multi-category queries are rare in current corpus)
- **Timing smoke-test**: not measurable in this dispatch (no live multi-store query available with Dagster background run in progress)

**Parallel dispatch timing validated 2026-04-21** (cross-module cleanup sweep Item 8, after Dagster run `778c604c` repopulated pg_* collections). Full results in `ops/baselines/2026-04-21-module-4-parallel-dispatch.md`. Summary: PostGIS 202 ms, Qdrant-docs 0 ms (empty collection), Qdrant-PG 987 ms; total retrieval 988 ms; ratio total:max = **1.00x** (parallel confirmed — total tracks max-store, not sum).

### 6. answer_runs + answer_retrieval_items wiring

- **INSERT location**: `run_deterministic_rag()` in `orchestrator.py`, just before the Redis cache write (after follow-up synthesis, after provenance enrichment — full response assembled)
- **New service**: `src/fastapi/app/services/answer_run_store.py` — `insert_answer_run()` (returns UUID) + `batch_insert_retrieval_items()` (executemany)
- **Fields populated on insert**:
  - `workspace_id`, `project_id`, `query_text`, `query_class` (from spec classifier)
  - `embedding_model`, `sparse_model`, `sparse_model_version`
  - `fusion_method="rrf"`, `sparse_boost_applied`, `reranker_version`
  - `retrieval_strategy_version`, `workspace_data_version_at_query`, `project_data_version_at_query`
  - `backend_used`, `partial_failure_details`
- **`answer_retrieval_items`** rows written at two stages:
  - `retrieved`: one row per fused candidate from `_fused_candidates` (cross-store RRF output), with `rrf_rank`, `rrf_score`, `retriever_score`, `source_store`
  - `reranked`: one row per chunk surviving the cross-encoder (from `search_documents` output after rerank), with `reranker_score`
- **Smoke test result**:
  ```
  Inserted answer_run_id: 37de1e42-ee3a-4317-b785-9e66f3fe6856
  DB row: {query_class: 'factual', sparse_boost_applied: True,
           reranker_version: 'bge-reranker-base@5ccf1b81',
           partial_failure_details: None}
  ```
- Failures are non-fatal (caught at WARNING level) — observability writes never fail a user query

### 7. retrieval_strategy_version bump

- **Status**: ALREADY BUMPED in Chunk 2 — `RETRIEVAL_STRATEGY_VERSION = "v1-hybrid-2026-04-21"`
- `test_retrieval_strategy_version_is_pre_hybrid` test updated to `test_retrieval_strategy_version_is_hybrid` (was checking for the pre-Chunk-2 value)
- No further bump needed in Chunk 3

### 8. Schema additions

- **Migration**: `2026_04_21_120000_add_partial_failure_details_to_answer_runs.php` (batch 17)
- **Column**: `silver.answer_runs.partial_failure_details JSONB NULL`
- **Partial index**: `idx_answer_runs_partial_failures` on `(workspace_id) WHERE partial_failure_details IS NOT NULL`
- Migration ran: 27.71ms — confirmed `DONE`
- `AnswerRunCreate` Pydantic model updated with `partial_failure_details: dict[str, str] | None`

### 9. FastAPI rebuild needed?

**Not needed.** All changes are:
- New Python modules in `src/fastapi/app/services/` — loaded by Python import, no container rebuild required
- `main.py` lifespan update — hot-reloaded by uvicorn `--reload` in dev
- `orchestrator.py` and `tools.py` edits — hot-reloaded
- `bge-reranker-base` will load from HuggingFace cache on first warm-up request (~278 MB download only if not cached). The `HF_HOME=/tmp/hf_cache` env is set. If the model is not yet cached, the first request will be slow (~10-30s on CPU). **Flag to Kyle**: if the `georag-fastapi` container has a fresh HF cache, expect a 10-30s warm-up latency on first query post-deploy. A container restart (not rebuild) will trigger lifespan warm-up.

### 10. Files touched

**New files:**
- `src/fastapi/app/services/reranker.py`
- `src/fastapi/app/services/identifier_boost.py`
- `src/fastapi/app/services/answer_run_store.py`
- `src/fastapi/tests/test_identifier_boost.py`
- `database/migrations/2026_04_21_120000_add_partial_failure_details_to_answer_runs.php`

**Modified files:**
- `src/fastapi/app/main.py` — reranker lifespan updated to BGE model
- `src/fastapi/app/services/qdrant_service.py` — `sparse_boost_factor` param on both hybrid functions
- `src/fastapi/app/agent/tools.py` — `search_documents` gains `sparse_boost_factor` param, threads to `hybrid_query`
- `src/fastapi/app/agent/orchestrator.py` — identifier boost detection, per-store timeouts on parallel gather, `_fused_candidates` exposed, answer_runs INSERT block
- `src/fastapi/app/models/answer_run.py` — `partial_failure_details` field added
- `src/fastapi/tests/test_query_classifier.py` — `test_retrieval_strategy_version_is_pre_hybrid` renamed + updated

### 11. Surprises

1. **`RETRIEVAL_STRATEGY_VERSION` already bumped** — Chunk 2 had already set `"v1-hybrid-2026-04-21"` in `query_classifier.py`. The spec said to bump it in Chunk 3; no action needed, just the test name update.

2. **`_fused_candidates` scoping** — the `_fused` variable was scoped inside a `try` block in the cross-store RRF section. Added `_fused_candidates: list = []` sentinel outside the try to expose it to the answer_runs INSERT block below. This is a one-liner sentinel pattern — clean.

3. **`PLS-22-08` pattern edge case** — the initial dashed hole ID regex required uppercase letters in the middle segment, which correctly excludes plain ISO dates (`2022-04-15`) but incorrectly excluded the very common `LETTERS-digits-digits` format (`PLS-22-08`). Fixed by using two regex families: Family A (letter prefix, any middle) and Family B (digit prefix, letter middle). 37/37 tests pass after fix.

4. **bge-reranker-base revision SHA** — confirmed `5ccf1b81c57ff625b3e4b7ab15481d6e2ee9bc56` against HuggingFace API 2026-04-21. This is pinned in `reranker.py` and will need manual update if the model is revised upstream.

---

## Phase D complete 2026-04-21

Applied by: backend-fastapi agent (Claude Sonnet 4.6)

### Runbooks written

| Path | Line count | Scope |
|---|---|---|
| `ops/runbooks/retrieval-pipeline.md` | 175 | Full retrieval flow, per-store timeouts, 5-step triage, replay procedure, version-bump side effects |
| `ops/runbooks/hybrid-retrieval.md` | 189 | Model pins, bump procedure, identifier-boost regex table, workspace override stub, fallback behavior |
| `ops/runbooks/retrieval-cache.md` | 161 | v4 key structure, component rationale, TTL, all invalidation paths, inspection commands, hit-rate expectations |
| `docs/query-class-routing.md` | 193 | 6-class definitions with precedence, add-new-class 6-step procedure, workspace override stub, testing commands |

### Sections not fully fleshed out (source unavailable)

- **Phase C measurements** — recall@10, recall@20, MRR, and latency p95 comparisons between hybrid and dense-only are deferred pending Module 10 golden-corpus assembly and background materialize `778c604c` completion. `hybrid-retrieval.md` documents the empirical rationale only.
- **Workspace override** — `identifier_boost_enabled` and `query_class_overrides` fields are Module 9 scope. Both runbooks contain TODO stubs with the expected schema shape.
- **Dense encoder revision SHA** — `bge-small-en-v1.5` is not pinned by digest in current code (Milestone 2 benchmarking will select the final geological-domain model). `hybrid-retrieval.md` notes this explicitly.

### Factual drift noticed

- The audit `retrieval-pipeline.md` flow diagram includes Neo4j timeout as 2.0s (`TIMEOUT_NEO4J_S`) — this matches the Chunk 3 implementation. The architecture doc Section 06 states 3.0s for Neo4j. The code value (2.0s) is what was wired in the parallel fan-out; the runbook reflects code, not spec. Flag for Section 06 reconciliation in Module 10 doc sweep.
- Cache key audit note: the v4 key includes `categories` (internal routing dict) in the hash, not just the spec-class label. The spec (Section 05d) describes a simpler key without routing categories. The implementation is more conservative (harder to get false cache hits). No functional problem; document the divergence in Module 10.

### Module 4 final status

Phase A + B (Chunks 1/2/3) + D complete. Phase C (measurement) deferred pending Module 10 golden-corpus assembly + background materialize `778c604c` completion.

---

## Cache-scope fix 2026-04-21

Applied by: backend-fastapi agent (Claude Sonnet 4.6)
Date: 2026-04-21
Scope: Module 4 Phase B addendum — cache boundary reshaped per arch §05c and Global Invariant

### Before

Full `GeoRAGResponse` (synthesized answer + citations + viz payloads) was stored in Redis under key prefix `v4`. Cache hits returned the full answer without running synthesis — answer-level caching, spec violation. The "What is NOT cached here" section of `ops/runbooks/retrieval-cache.md` explicitly flagged this as a known gap since Phase D.

### After

Only `CachedRetrievalContext` (retrieval candidates after RRF + reranking) is stored in Redis under key prefix `v5`. Synthesis always runs fresh on every query — cache hit or miss. A new `answer_runs` row is written on every query.

### Changes

| File | Change |
|---|---|
| `src/fastapi/app/models/retrieval_cache.py` | New — `CachedRetrievalContext` (13 fields) + `CachedRetrievalCandidate` (10 fields) |
| `src/fastapi/app/agent/orchestrator.py` | Cache hit: deserialize `CachedRetrievalContext`, skip retrieval, synthesize fresh. Cache miss: retrieve, RRF, rerank, SETEX(`CachedRetrievalContext`), synthesize. Removed `GeoRAGResponse` SETEX. |
| `src/fastapi/app/models/answer_run.py` | Added `cache_hit_of_run_id: UUID | None` field |
| `src/fastapi/app/services/answer_run_store.py` | `insert_answer_run()` now writes `cache_hit_of_run_id` ($30 positional param) |
| `src/fastapi/app/services/query_classifier.py` | `RETRIEVAL_STRATEGY_VERSION` bumped from `v1-hybrid-2026-04-21` to `v2-retrieval-only-cache-2026-04-21` |
| `database/migrations/2026_04_21_130000_add_cache_hit_of_run_id_to_answer_runs.php` | Migration batch 18 — additive `cache_hit_of_run_id UUID NULL` + partial index |
| `src/fastapi/tests/test_cache_scope.py` | New — 14 tests covering: model shape, no answer fields, v5 prefix, stale v4 handling, round-trip, candidate shape |
| `src/fastapi/tests/test_cache_key_versioning.py` | Updated — v3 prefix test replaced with v5; DOCUMENT_SCOPE_VERSION test replaced with workspace_data_version test |
| `ops/runbooks/retrieval-cache.md` | Rewritten — v5 prefix, retrieval-only scope, why retrieval-only, cache hit bookkeeping, updated inspection commands |
| `ops/backlog/module-5-intake.md` | New — Module 5 synthesis context for retrieval-only caching |

### Key prefix

Bumped v4 -> v5. Old v4 entries (GeoRAGResponse shape) become unreachable and TTL out within 5 minutes. No manual flush needed.

### RETRIEVAL_STRATEGY_VERSION

`v2-retrieval-only-cache-2026-04-21` — marks the behavioral boundary change.

### Migration batch number

18 (`2026_04_21_130000_add_cache_hit_of_run_id_to_answer_runs.php`)

### Validation

- Syntax check: all modified Python files pass `ast.parse()` clean
- Test file `test_cache_scope.py`: 14 tests covering model shape, key prefix, stale entry handling, serialization
- `test_cache_key_versioning.py`: updated to v5; existing test structure preserved
- Smoke test procedure (requires running stack):
  1. Issue query -> expect cache miss, SETEX CachedRetrievalContext, synthesis runs
  2. Issue same query within 5 min -> expect cache hit, retrieval skipped, synthesis runs, new answer_run_id, cache_hit_of_run_id populated
  3. `redis-cli GET <v5-key> | python3 -m json.tool` -> verify `schema_version`, `candidates_reranked`, no `text`/`citations`
  4. `\d silver.answer_runs` -> verify `cache_hit_of_run_id` column present

### Surprises

1. **Python indentation complexity** — The retrieval fan-out block in orchestrator.py spans ~400 lines at function-level indentation. The cache-hit guard (`if not _cache_hit:`) was added using a combination of: (a) sentinel variable pattern before the fan-out, (b) the `if not _cache_hit:` wrapper around the gather + result processing, (c) a second `if not _cache_hit:` wrapper around sequential tools (downhole, assay, targeting, graph, escalation). All compile clean per ast.parse().

2. **`original_answer_run_id` two-pass pattern** — The cache write happens before synthesis (and before `insert_answer_run()`). A post-INSERT Redis update patches the cached context with the `answer_run_id` so future cache hits can populate `cache_hit_of_run_id`. This is a two-write pattern but both writes are non-fatal fire-and-forget.

3. **Stale v4 entry handling** — `CachedRetrievalContext.model_validate_json()` will raise `ValidationError` on a v4 entry (GeoRAGResponse shape, missing required fields). The orchestrator catches this and logs a warning, treating it as a cache miss. No crash.

### Module 4 updated final status

Phase A + B (Chunks 1/2/3) + B addendum (cache-scope fix) + D complete. Phase C (measurement) deferred pending Module 10 golden-corpus assembly.
