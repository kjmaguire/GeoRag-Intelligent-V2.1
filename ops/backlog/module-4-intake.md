# Module 4 (RAG Retrieval & Caching) — pre-approved intake items

Items flagged during Modules 1–2 that Kyle has pre-approved for Module 4 execution.
Landed here as canonical handoff so Module 4 Phase A picks them up first.

## PostGIS geography-cast bypasses GIST index

- **Raised:** 2026-04-20 Module 2 Phase C baselines
- **Source:** `ops/baselines/2026-04-20-datastores-baselines.md` C1 PostGIS section
- **Finding:** `ST_DWithin(geom::geography, ...)` forces a parallel seq scan because the `::geography` cast strips the native geometry GIST index. A 50km collar-near-point query on 33,490 rows takes 117ms via seq scan vs the geometry-native equivalent which uses GIST (demonstrated at 3ms on a 100m proximity join).
- **Impact:** Every retrieval query that computes geodesic distance on a large table will scale poorly unless the index story is fixed. This is a hot path for collar search, NI 43-101 report proximity, historic working location lookups.
- **Two resolution options:**
  - **(a)** Add a functional GIST index: `CREATE INDEX ... USING GIST ((geom::geography))` on each relevant table. Trade-off: double index storage, but geography-native queries stay clean and accurate for geodesic distance.
  - **(b)** Standardize retrieval on geometry-native `ST_DWithin` with explicit bbox pre-filter for the geodesic distance. Trade-off: code complexity + precision caveat for distances near the dateline / poles (not an issue for mining regions in NA/AUS).
- **Approval:** Kyle pre-approved 2026-04-20. Module 4 Phase A picks the option during retrieval strategy design.
- **Owner:** backend-fastapi / data-engineer during Module 4 Phase A

---

## Silver-trapped data needs structured_record evidence wiring

- **Raised:** 2026-04-20 Module 3 Chunk 1 close-out
- **Source:** `ops/audit/2026-04-20-ingestion-audit.md` Chunk 1 deferred items
- **Finding:** `silver.seismic_surveys` (1 row, SEG-Y) and `silver.geochemistry` (344 rows, XYZ points) are ingested into Silver but have zero RAG-path consumers. No evidence_items rows, no retrieval queries, no citation path.
- **Impact:** Any user question about geochem results or seismic surveys returns empty — the data exists but is unreachable through RAG.
- **Approach:** When Module 4 implements retrieval, include:
  1. structured_record evidence_items writes for both tables (per addendum §04j — Module 3 already provides the evidence-model substrate; Module 4 just emits the rows)
  2. PostGIS query paths for geochem proximity and seismic bbox intersection
  3. A decision on SEG-Y: whether to include in V1 retrieval (spec says V1-roadmap deferred) or continue to suppress the 1 row
- **Approval:** pre-approved 2026-04-20 for Module 4 Phase A consideration
- **Owner:** backend-fastapi + data-engineer during Module 4

---

## Phase B Chunk 2 close-out note (2026-04-21)

Phase B Chunk 2 landed hybrid infrastructure:
- SPLADE++ sparse encoder live (query-side + doc-side, naver/splade-cocondenser-ensembledistil@49cf4c7b)
- `index_reports.py` wired for multi-vector upserts (dense "" + sparse "text" named slots)
- Qdrant Query API migration complete (Prefetch + FusionQuery RRF, all 5 collections recreated)
- Cross-store RRF implemented in `src/fastapi/app/services/fusion.py` + integrated in orchestrator
- FastAPI memory bumped 4G -> 6G; container recreated, confirmed 6 GiB limit
- 19 RRF unit tests passing

**Silver-trapped structured_record evidence wiring still pending** -- flagged for Chunk 3 or follow-up.
`silver.geochemistry` (344 rows) and `silver.seismic_surveys` (1 row) have no RAG consumers,
no evidence_items, no retrieval tool paths. The PG-05 intake item from Phase A remains open.

---

## Chunk 2 data-recovery follow-ups (2026-04-21)

### C3-01 — RESOLVED (2026-04-21 Chunk 2 cleanup)

- **Was**: Dagster daemon memory limit 1 GiB insufficient for SPLADE (~440 MB) + bge-small (~100 MB) + daemon base (~270 MB). `index_reports` OOM-killed.
- **Fix applied**: `docker-compose.yml` `dagster-daemon` service bumped to `memory: 3G` limit / `1G` reservation. Webserver bumped from 1G to 2G simultaneously.
- `index_reports` re-ran successfully after recreate: run `7df26b22`, 18 sections, 18 points upserted to `georag_reports`.
- Asset checks passed: `embedding_id_present`, `parser_error_floor` (100% ratio).

### C3-02 — RESOLVED (2026-04-21 Chunk 2 cleanup)

- **Was**: `index_public_geoscience_qdrant` writing dense-only `PointStruct(vector=float_list)` with no `workspace_id` or `parser_version` in payload. `_ensure_collection()` else-branch had `optimizer_config=` typo (wrong kwarg).
- **Fix applied**: `src/dagster/georag_dagster/assets/index_public_geoscience.py`
  - Imported `encode_sparse_batch`, `SPARSE_MODEL_VERSION` from `sparse_encoder`
  - Added `DEFAULT_WORKSPACE_UUID = "a0000000-0000-0000-0000-000000000001"` constant
  - `_ensure_collection()`: create branch now uses named-vector dict config (`""` dense + `"text"` sparse); `SparseIndexParams`/`SparseVectorParams` imported. Else-branch typo fixed: `optimizer_config=` → `optimizers_config=`
  - All 6 payload builder functions (`_mine_payload`, `_occurrence_payload`, `_drillhole_payload`, `_rock_sample_payload`, `_assessment_survey_payload`, `_resource_potential_payload`) now include `workspace_id` and `parser_version` fields
  - `_run_canonical_type()`: now calls `encode_sparse_batch()`, builds `vector_payload = {"": dense, "text": SparseVector(...)}` dict, uses named-vector PointStruct
  - MaterializeResult metadata now includes `sparse_model` field
- Background re-run launched as `778c604c` (STARTED ~17:55 UTC). pg_mine (140 rows) completed and validated. pg_mineral_occurrence (22,229) in progress, ~85 min CPU-only estimate.

### C3-03 — RESOLVED (2026-04-21 Chunk 2 cleanup)

- **Was**: Ghost runs `44f3a058` and `460a6c3a` stuck in STARTED state with no end_time. Third ghost `03bde662` also found.
- **Fix applied**: All three runs marked CANCELED via direct DB update in `georag_dagster`:
  `UPDATE runs SET status='CANCELED', end_time=..., update_timestamp=... WHERE run_id IN (...);`
- Zero STARTED runs remain in the instance.
