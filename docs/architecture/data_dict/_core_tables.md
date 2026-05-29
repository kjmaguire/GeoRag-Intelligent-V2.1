# Data Dictionary — Core Tables (hand-curated)

Five highest-traffic tables with full column lists, verified against the
live migrations. The other tables stay in the per-schema skeletons until
the [generator](../appendix/F-data-dictionary.md) ships.

---

## `silver.collars`

Created: [database/migrations/2026_04_09_180100_create_collars_table.php:15](../../../database/migrations/2026_04_09_180100_create_collars_table.php).
Geometry column added at line 34 (PostGIS `AddGeometryColumn`).
Spatial uncertainty added by [2026_05_23_050000](../../../database/migrations/2026_05_23_050000_add_spatial_uncertainty_to_collars_and_spatial_features.php).
`workspace_id` backfilled by [phase0/96-rls-tenant-isolation-block1.sql:79-84](../../../database/raw/phase0/96-rls-tenant-isolation-block1.sql).

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `collar_id` | UUID | no | (PK) | Primary key |
| `hole_id` | VARCHAR(50) | no | — | Vendor / operator hole ID; unique within project |
| `project_id` | UUID | no | — | FK `silver.projects` ON DELETE CASCADE |
| `easting` | FLOAT | no | — | In project CRS (default EPSG:32613) |
| `northing` | FLOAT | no | — | In project CRS |
| `elevation` | FLOAT | yes | — | metres ASL |
| `total_depth` | FLOAT | no | — | metres |
| `hole_type` | VARCHAR(20) | no | — | `dd`, `rc`, `ac`, `rab`, etc. |
| `azimuth` | FLOAT | yes | — | degrees from N |
| `dip` | FLOAT | yes | — | degrees from horizontal |
| `drill_date` | DATE | yes | — | |
| `status` | VARCHAR(20) | no | — | `planned`, `drilling`, `completed`, `abandoned` |
| `geom` | `GEOMETRY(POINT, 32613)` | yes | — | PostGIS; GIST index `idx_collars_geom` |
| `workspace_id` | UUID | no | — | RLS fence; FK `silver.workspaces` ON DELETE CASCADE |
| `spatial_uncertainty_m` | NUMERIC(8,2) | yes | — | Added 2026-05-23 |
| `data_version` | BIGINT | no | 1 | Captured at write time |
| `created_at`, `updated_at` | TIMESTAMPTZ | no | `clock_timestamp()` | |

**Indexes:** `silver_collars_project_hole_key` UNIQUE `(project_id, hole_id)`;
`idx_collars_geom` GIST `(geom)`.

**RLS:** enabled + FORCE; policy
`workspace_id = current_setting('app.workspace_id', true)::uuid`.

**Read by:** Martin `silver.pg_collars_by_project`; frontend
DrillholeDetail / HoleCompare / Lakehouse / Workspace; Neo4j
`:DrillHole` node via `index_neo4j` Dagster asset; RAG
`query_collar_details` tool.

---

## `silver.answer_runs`

Created: [database/migrations/2026_04_21_100000_create_answer_runs.php:56](../../../database/migrations/2026_04_21_100000_create_answer_runs.php).
Hallucination-guard results added by [2026_05_20_020000](../../../database/migrations/2026_05_20_020000_add_hallucination_guard_results_to_answer_runs.php).
Lineage columns added by [2026_05_21_010000](../../../database/migrations/2026_05_21_010000_add_lineage_columns_to_answer_runs.php).
Confidence + latency added by [2026_05_25_200000](../../../database/migrations/2026_05_25_200000_add_confidence_and_latency_ms_to_answer_runs.php).

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `answer_run_id` | UUID | no | `gen_random_uuid()` | PK |
| `workspace_id` | UUID | no | — | FK `silver.workspaces` ON DELETE CASCADE |
| `project_id` | UUID | yes | — | FK `silver.projects` ON DELETE SET NULL |
| `user_id` | BIGINT | yes | — | FK `public.users` ON DELETE RESTRICT |
| `query_text` | TEXT | no | — | The raw user query |
| `query_class` | VARCHAR(32) | no | — | CHECK in `factual\|spatial\|document\|computation\|viz\|unknown` |
| `embedding_model` | VARCHAR(128) | yes | — | e.g., `BAAI/bge-small-en-v1.5` |
| `embedding_model_version` | VARCHAR(64) | yes | — | HF revision SHA |
| `sparse_model` | VARCHAR(128) | yes | — | e.g., `naver/splade-cocondenser-ensembledistil` |
| `sparse_model_version` | VARCHAR(64) | yes | — | |
| `fusion_method` | VARCHAR(16) | yes | — | CHECK in `rrf\|dbsf` |
| `sparse_boost_applied` | BOOLEAN | yes | — | |
| `reranker_version` | VARCHAR(64) | yes | — | |
| `retrieval_strategy_version` | VARCHAR(32) | yes | — | |
| `workspace_data_version_at_query` | BIGINT | no | — | Captured at query time |
| `project_data_version_at_query` | BIGINT | yes | — | |
| `backend_used` | VARCHAR(32) | yes | — | CHECK in `vllm\|ollama\|anthropic` |
| `backend_chain` | TEXT[] | yes | — | Cross-backend failover trail |
| `model_name` | VARCHAR(128) | yes | — | e.g., `Qwen/Qwen3-14B-AWQ` |
| `input_tokens`, `output_tokens` | INTEGER | yes | — | |
| `cache_read_tokens`, `cache_creation_tokens` | INTEGER | yes | — | Anthropic prompt-caching telemetry |
| `speculative_acceptance_rate_sample` | NUMERIC(6,4) | yes | — | vLLM spec-decoding telemetry |
| `evidence_truncated_count` | INTEGER | yes | — | |
| `citation_lifecycle_state` | (varchar) | yes | — | `pending\|resolved\|broken\|refused` |
| `trace_id` | TEXT | yes | — | W3C trace context for Tempo join |
| `root_span_id` | TEXT | yes | — | |
| (later additions) | — | — | — | hallucination_guard_results, confidence, latency_ms |
| `created_at` | TIMESTAMPTZ | no | `clock_timestamp()` | |

**Indexes:** `(workspace_id)`, `(project_id)`, `(created_at DESC)`,
partial `(trace_id) WHERE trace_id IS NOT NULL`, `(query_class)`.

**Constraints:** `answer_runs_query_class_valid` CHECK on `query_class`
(line 107); FK cascades as above.

**RLS:** enabled + FORCE; standard workspace policy.

**Children:** `silver.answer_retrieval_items`,
`silver.answer_citation_items`, `silver.answer_citation_spans`,
`silver.message_feedback` (all cascade on delete).

---

## `audit.audit_ledger`

Created: [database/raw/phase0/20-layer-b-audit-ledger.sql:25](../../../database/raw/phase0/20-layer-b-audit-ledger.sql).
Partitioned monthly via pg_partman (24-month retention).

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | UUID | no | `gen_random_uuid()` | Part of PK |
| `workspace_id` | UUID | yes | — | NULL for system-global events |
| `actor_id` | BIGINT | yes | — | FK `public.users.id` (logical; no DB-level FK due to partitioning) |
| `actor_kind` | TEXT | no | `'user'` | CHECK in `user\|system\|agent\|workflow\|external` |
| `action_type` | TEXT | no | — | Canonical: `agent.invoke`, `workspace.create`, `report.signoff`, `storage.tier_transition`, … |
| `target_schema` | TEXT | yes | — | e.g., `silver` |
| `target_table` | TEXT | yes | — | e.g., `collars` |
| `target_id` | TEXT | yes | — | Stringified PK of the target |
| `payload` | JSONB | no | `'{}'::jsonb` | Action-specific body (canonical JSON for hashing) |
| `previous_hash` | BYTEA | yes | — | Pointer to prior workspace row's hash |
| `hash` | BYTEA | yes | — | `sha256(previous_hash \|\| actor_id \|\| action_type \|\| … \|\| payload::json \|\| created_at_iso)` |
| `trace_id` | TEXT | yes | — | Joins to Tempo + `silver.answer_runs.trace_id` |
| `created_at` | TIMESTAMPTZ | no | `clock_timestamp()` | NOT `now()` — strict monotonic within transaction |

**PK:** `(id, created_at)` (composite — required by `PARTITION BY RANGE (created_at)`).

**Indexes:** `(workspace_id, created_at DESC)`,
`(action_type, created_at DESC)`,
`(target_schema, target_table, target_id)`,
partial `(trace_id) WHERE trace_id IS NOT NULL`.

**Trigger:** `audit_ledger_compute_hash_trg` (BEFORE INSERT) — calls
`audit.compute_audit_hash()`, locks prior row of same workspace
`FOR UPDATE`, computes SHA-256 of canonical message.

**Partitioning:** monthly via `pg_partman.create_parent('audit.audit_ledger', 'created_at', 'native', 'monthly')` with 24-month retention and infinite partitions ahead.

**RLS:** enabled (tightened by [phase0/99-rls-block3-policy-tighten.sql:14](../../../database/raw/phase0/99-rls-block3-policy-tighten.sql)).

---

## `bronze.provenance`

Created: [database/migrations/2026_04_18_130000_create_bronze_provenance_table.php:22](../../../database/migrations/2026_04_18_130000_create_bronze_provenance_table.php).
Auto-fill trigger added [2026_05_25_175601](../../../database/migrations/2026_05_25_175601_add_workspace_id_autopopulation_trigger_to_bronze_provenance.php).

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `provenance_id` | UUID | no | `gen_random_uuid()` | PK |
| `target_schema` | VARCHAR(32) | no | — | e.g., `silver` |
| `target_table` | VARCHAR(64) | no | — | e.g., `collars` |
| `target_id` | UUID | no | — | The silver/gold PK |
| `source_file` | TEXT | no | — | Logical source filename |
| `source_file_sha256` | CHAR(64) | no | — | SHA256 of bytes — dedup key |
| `source_row` | INTEGER | yes | — | For tabular sources |
| `source_col_map` | JSONB | yes | — | `{column_name → bronze.raw_*.column_value}` |
| `parser_name` | VARCHAR(64) | no | — | e.g., `pdf_report`, `csv_collar` |
| `parser_version` | VARCHAR(32) | no | — | semver |
| `ingested_at` | TIMESTAMPTZ | no | `NOW()` | |
| `ingest_run_id` | UUID | yes | — | FK `bronze.ingest_runs.run_id` |
| `workspace_id` | UUID | no (since May 25) | — | Auto-filled by trigger from target row |

**Indexes:**
- `idx_provenance_target (target_schema, target_table, target_id)` — primary lookup.
- `idx_provenance_sha256 (source_file_sha256)` — dedup.
- `idx_provenance_ingested_at` — recent-activity.
- Partial on `ingest_run_id IS NOT NULL`.

**Trigger:** `provenance_autofill_workspace_id_trg` — BEFORE INSERT.
Resolves `workspace_id` from the target silver row by `target_schema +
target_table + target_id` via CASE over 8 silver tables (collars,
samples, lithology_logs, reports, spatial_features, raster_layers,
geophysics_surveys, assays_v2). `EXCEPTION WHEN OTHERS RETURN NEW` — a
missing target never blocks a provenance insert.

**RLS:** enabled (May 2026 bronze tenancy sweep).

---

## `gold.significant_intersections`

Created: [database/migrations/2026_05_20_060700_create_gold_drillhole_tables.php](../../../database/migrations/2026_05_20_060700_create_gold_drillhole_tables.php) §2.

| Column | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | UUID | no | `gen_random_uuid()` | PK |
| `workspace_id` | UUID | no | — | RLS fence |
| `collar_id` | UUID | no | — | FK `silver.collars` |
| `element` | TEXT | no | — | `Au`, `Cu`, `U3O8`, … |
| `cutoff_grade` | NUMERIC | no | — | Threshold used to compute the intersection |
| `from_depth` | NUMERIC | no | — | metres |
| `to_depth` | NUMERIC | no | — | metres |
| `true_width_m` | NUMERIC | yes | — | When azimuth + dip known |
| `downhole_length` | NUMERIC | yes | GENERATED `(to_depth - from_depth) STORED` | Materialised |
| `weighted_avg` | NUMERIC | no | — | Length-weighted grade |
| `unit` | TEXT | no | — | `g/t`, `ppm`, `pct`, etc. |
| `peak_value` | NUMERIC | yes | — | Highest single sample within the interval |
| `peak_depth` | NUMERIC | yes | — | metres |
| `zone_name` | TEXT | yes | — | Optional mineralisation zone tag |
| `computed_at` | TIMESTAMPTZ | no | `now()` | |

**Indexes:**
- `gold_significant_intersections_workspace_element_idx (workspace_id, element)`
- `gold_significant_intersections_collar_idx (workspace_id, collar_id)`
- `gold_significant_intersections_workspace_id_idx (workspace_id)`

**Writer:** Dagster `silver_to_gold/significant_intersections` asset.

**Read by:** Martin function `silver.significant_intersections_by_project`
([2026_05_20_061000](../../../database/migrations/2026_05_20_061000_create_martin_significant_intersections_function.php))
joined to `silver.collars`; frontend Targets / DrillholeDetail.

**RLS:** enabled + FORCE; standard workspace policy.
