# Ingestion Pipeline Runbook

Operates the Dagster Bronze → Silver → Gold → Index pipeline. Use this when triggering runs, replaying from Bronze, debugging a stuck run, or understanding the commit gate.

---

## Asset graph at a glance

```
Bronze (21 assets)          Silver (22 assets)         Gold (3)      Index (4)      Commit
─────────────────────       ──────────────────────     ────────────  ───────────    ──────
bronze_collars          →   silver_collars             gold_placeholder → index_placeholder
bronze_surveys          →   silver_surveys                                              │
bronze_lithology        →   silver_lithology                                            │
bronze_samples          →   silver_samples                                              │
bronze_well_logs        →   silver_well_logs                                            │
bronze_spatial          →   silver_spatial                                              │
bronze_reports          →   silver_reports      ──────────────────→ index_reports       │
bronze_xlsx             →   silver_xlsx                                                 │
bronze_seismic          →   silver_seismic  (*)                                         │
bronze_xyz              →   silver_xyz      (*)                                         │
                            silver_raster   (**)                                        │
bronze_raster_uploads   →   silver_cog_rasters                                          │
silver_collars          →   silver_drill_traces                                         │
                            silver_collars   ──────────────────→  index_neo4j           │
bronze_pg_ca_sk_* (11)  →   silver_pg_ca_sk_* (11) → gold_public_geoscience_neo4j
                                                     → gold_cross_corpus_linker
                                                     → index_public_geoscience_qdrant
                                                                                        ↓
                                                                          commit_ingestion_run
```

`(*)` silver_seismic + silver_xyz are Silver-trapped graph dead-ends — no Gold/Index downstream yet. Retrieval wiring tracked in `ops/backlog/module-4-intake.md` and `ops/backlog/module-8-intake.md`.

`(**)` silver_raster has no Bronze asset dependency declared (orphan). Full upload wiring is a later sprint.

**Totals (as of Module 3 Phase B):** 54 assets, 26 asset checks, 1 commit gate.

Asset check map — see the dedicated table in the "Asset-check map" section below.

---

## Dagster UI

```
Service: georag-dagster-webserver
URL:     http://localhost:3001
Profile: started with --profile dev-ingest
```

Start if not running:

```bash
docker compose --profile dev-ingest up -d georag-dagster-webserver georag-dagster-daemon
```

Verify healthy:

```bash
docker compose ps georag-dagster-webserver georag-dagster-daemon
```

---

## How to trigger a run

### Via Dagster UI

1. Open `http://localhost:3001`
2. Navigate to Assets → select assets → Materialize Selected
3. For the full private-project pipeline: select all assets in the `bronze`, `silver`, `gold`, `index`, and `commit` groups

### Via CLI (Dagster tool inside container)

Materialize all assets:

```bash
docker exec georag-dagster-daemon \
  dagster asset materialize --select '*' -m georag_dagster
```

Materialize a single asset:

```bash
docker exec georag-dagster-daemon \
  dagster asset materialize --select 'silver_collars' -m georag_dagster
```

Materialize the commit gate with a specific workspace and projects:

```bash
docker exec georag-dagster-daemon \
  dagster asset materialize \
    --select 'commit_ingestion_run' \
    --config '{"ops": {"commit_ingestion_run": {"config": {"workspace_id": "a0000000-0000-0000-0000-000000000001", "project_ids": "<uuid1>,<uuid2>"}}}}' \
    -m georag_dagster
```

### Schedules

All three schedules are STOPPED by default. Enable via Dagster UI → Automation → Schedules.

| Schedule | Cron | Target | Purpose |
|---|---|---|---|
| `full_ingest_schedule` | `0 2 * * *` | All assets | Daily full private-project ingest |
| `public_geoscience_weekly_refresh` | `0 3 * * 0` | `_PG_ACTIVE_ASSETS` (SK + BC) | Force full pull regardless of edit date |
| `public_geoscience_daily_edit_check` | `30 5 * * *` | `_PG_ACTIVE_ASSETS` | Skip-if-unchanged daily check |

Production note: `full_ingest_schedule` targets `AssetSelection.all()`, which includes public-geoscience assets. Before enabling in production, confirm this coupling is acceptable or narrow the selection to private-project assets only.

### Auto-materialize

Auto-materialize is not currently configured on any asset. No assets will self-trigger without a manual run or schedule enable.

### Sensor-triggered ingestion

`minio_upload_sensor` polls the `bronze` SeaweedFS bucket every 5 minutes (300 s interval). When a new object appears (last-modified time > cursor), it triggers materialization of the corresponding Bronze asset based on path prefix:

| Prefix | Asset triggered |
|---|---|
| `collars/` | `bronze_collars` |
| `surveys/` | `bronze_surveys` |
| `lithology/` | `bronze_lithology` |
| `samples/` | `bronze_samples` |
| `well_logs/` | `bronze_well_logs` |
| `spatial/` | `bronze_spatial` |
| `reports/` | `bronze_reports` |
| `excel/` | `bronze_xlsx` |
| `seismic/` | `bronze_seismic` |
| `xyz/` | `bronze_xyz` |

The sensor also reads the `x-georag-vendor-profile-id` object metadata header and threads it to the paired Silver asset via `run_config`. If the header is absent, `vendor_profile_id=None` (backward-compatible).

Enable the sensor via Dagster UI → Automation → Sensors → `minio_upload_sensor` → Start.

---

## Replay from Bronze

Bronze files in SeaweedFS are immutable. Re-materializing a Silver or Gold asset always re-reads the Bronze object and re-parses it from scratch. This determinism guarantee: same Bronze SHA-256 + same parser version = same Silver/Gold/Index output.

**Replay one Silver asset (e.g. after a parser bugfix):**

```bash
docker exec georag-dagster-daemon \
  dagster asset materialize --select 'silver_collars' -m georag_dagster
```

**Replay a subset of the pipeline (Silver + downstream):**

```bash
docker exec georag-dagster-daemon \
  dagster asset materialize \
    --select 'silver_collars silver_surveys silver_lithology silver_samples' \
    -m georag_dagster
```

**Replay the full private-project chain:**

```bash
docker exec georag-dagster-daemon \
  dagster asset materialize \
    --select 'silver_collars silver_surveys silver_lithology silver_samples silver_well_logs silver_spatial silver_reports silver_xlsx silver_drill_traces silver_cog_rasters index_reports index_neo4j commit_ingestion_run' \
    -m georag_dagster
```

Reprocessing always starts from MinIO. Never re-materialize only Silver/Gold if the intent is to replay from an updated parser — that would not re-read the Bronze file. The Bronze asset must be re-materialized first (or already have been) to land a fresh Bronze object before Silver re-reads it.

---

## Debug a stuck run

### Tail daemon logs for errors:

```bash
docker compose logs georag-dagster-daemon --tail 200 | grep -iE 'ERROR|FAILURE|retry'
```

### Common causes and triage order:

1. **Asset check failure blocking the commit gate.**
   Check: Dagster UI → Assets → the stuck asset → "Checks" tab. A `blocking=True` check in FAILED state prevents `commit_ingestion_run` from executing. Fix the underlying data quality issue and re-materialize.

2. **FK violation from missing workspace.**
   Symptom: `commit_ingestion_run` fails with `psycopg2.IntegrityError: insert or update on table ... violates foreign key constraint`. Cause: the `workspace_id` in the run config does not exist in `silver.workspaces`.
   Fix: verify the workspace exists:
   ```sql
   SELECT workspace_id, name FROM silver.workspaces;
   ```
   Use `workspace_id = 'a0000000-0000-0000-0000-000000000001'` (the seeded default).

3. **S3 endpoint unreachable.**
   Symptom: Bronze assets fail immediately with `botocore.exceptions.EndpointResolutionError` or `ConnectionRefusedError`.
   Check: `docker compose ps georag-minio` — must be healthy. Verify env var: `docker exec georag-dagster-daemon env | grep S3_ENDPOINT_URL`.

4. **psycopg2 connection refused.**
   Symptom: Silver assets fail with `psycopg2.OperationalError: could not connect to server`.
   Check: `docker compose ps georag-pgbouncer` — must be healthy.

5. **Dagster daemon not running.**
   Symptom: runs are submitted but never start executing.
   Fix: `docker compose --profile dev-ingest up -d georag-dagster-daemon`.

### Inspect run state:

```bash
docker compose logs georag-dagster-daemon --tail 500 | grep -E 'RUN_ID|STARTED|FAILURE|SUCCESS'
```

Or navigate Dagster UI → Runs → click a run → view step-level logs.

---

## Re-ingest with a new parser version

Parser version flows from the asset code into `bronze.provenance.parser_version` (written at Bronze ingest time) and into `silver.document_revisions.parser_version` (written at Silver ingest time for document types).

**To force a replay with an updated parser:**

1. Bump the parser version string in the asset file (e.g. `PARSER_VERSION = "1.2.0"` in `assets/silver.py`).
2. Rebuild the Dagster image:
   ```bash
   docker compose build georag-dagster-daemon
   docker compose up -d --no-deps georag-dagster-daemon georag-dagster-webserver
   ```
3. Re-materialize the Bronze asset to confirm the Bronze file is current in MinIO (skip if unchanged).
4. Re-materialize the Silver asset:
   ```bash
   docker exec georag-dagster-daemon \
     dagster asset materialize --select 'silver_collars' -m georag_dagster
   ```

For document-type assets (`silver_reports`): the new parse creates a new `document_revisions` row with the bumped `parser_version` and incremented `revision_number`. The old revision is preserved with `superseded_by_revision_id` set to the new revision's ID.

For structured-data assets (collars, surveys, etc.): Silver uses `ON CONFLICT ... DO UPDATE`, so re-ingestion upserts rather than creating a new revision.

---

## The `commit_ingestion_run` gate

`commit_ingestion_run` is the terminal asset in the pipeline (`group_name="commit"`). It only executes when all upstream blocking asset checks pass — Dagster's structural enforcement, not hand-coded conditionals.

**What it does:**

1. Opens a single DB transaction and runs:
   ```sql
   UPDATE silver.workspaces SET data_version = data_version + 1
     WHERE workspace_id = :workspace_id;
   UPDATE projects SET data_version = data_version + 1
     WHERE id IN (:project_ids);
   ```
   Both UPDATEs are inside the same transaction — atomic.

2. Commits. If the monotonic trigger fires (rejects a decrement), the transaction rolls back and the asset fails — never silent.

3. Emits `workspace_data_version` and `project_data_versions` as materialization metadata for Module 7 Reverb consumption.

4. Runs post-ingest PostgreSQL tuning for each `_TUNE_TARGETS` table (CLUSTER + ANALYZE + MV refresh). Tuning runs outside the data_version transaction (CLUSTER takes ACCESS EXCLUSIVE lock). Tune failures are non-blocking — a failed CLUSTER logs a WARNING but does not roll back the committed data_version.

**Upstream deps (all must have passing blocking checks):**

- `silver_collars`, `silver_reports`, `silver_spatial`
- `index_reports`, `index_neo4j`
- `silver_drill_traces`, `silver_cog_rasters`

**_TUNE_TARGETS (post-ingest tune tables):**

| Table | Index | Materialized view |
|---|---|---|
| `silver.collars` | `idx_collars_geom` | `silver.mv_collar_summary` |
| `silver.reports` | `idx_reports_geom` | none |
| `silver.spatial_features` | `idx_spatial_features_geom` | none |
| `silver.drill_traces` | `idx_drill_traces_geom` | none |

---

## Asset-check map

26 checks total, all `blocking=True`.

| Asset | Check name | Assertion |
|---|---|---|
| `silver_collars` | `collar_count_positive` | `COUNT(*) > 0` in silver.collars |
| `silver_collars` | `schema_conformance_pass_rate` | `parse_ok > 0`; WARN at partial, ERROR at 0% |
| `silver_collars` | `crs_round_trip_sane` | Zero NULL geom + zero SRID=0 rows |
| `silver_surveys` | `parse_total_positive` | `COUNT(*) > 0` in silver.surveys |
| `silver_lithology` | `parse_total_positive` | `COUNT(*) > 0` in silver.lithology_logs |
| `silver_samples` | `parse_total_positive` | `COUNT(*) > 0` in silver.assay_samples |
| `silver_well_logs` | `parse_total_positive` | `COUNT(*) > 0` in silver.well_logs |
| `silver_spatial` | `geom_not_null` | Zero NULL geom rows in silver.spatial_features |
| `silver_spatial` | `crs_srid_populated` | Zero SRID=0 rows in silver.spatial_features |
| `silver_reports` | `parse_total_positive` | `COUNT(*) > 0` in silver.reports |
| `silver_reports` | `schema_conformance_pass_rate` | At least one report with non-empty sections_text |
| `silver_reports` | `no_duplicate_passage_ids` | Zero duplicate passage_id groups in silver.document_passages |
| `silver_reports` | `text_hash_sha256_valid` | All text_hash values match `^[0-9a-f]{64}$` |
| `silver_reports` | `document_revisions_document_id_not_null` | Zero NULL document_id in silver.document_revisions |
| `silver_reports` | `document_revisions_sha256_format` | All source_sha256 match `^[0-9a-f]{64}$` |
| `silver_reports` | `evidence_items_exactly_one_ref` | All evidence_items rows have exactly one non-null ref field |
| `silver_xlsx` | `parse_total_positive` | Combined collar+sample count > 0 (XLSX proxy) |
| `silver_seismic` | `parse_total_positive` | `COUNT(*) > 0` in the seismic Silver table |
| `silver_seismic` | `schema_conformance_pass_rate` | Partial/full parse quality |
| `silver_xyz` | `parse_total_positive` | `COUNT(*) > 0` in the XYZ Silver table |
| `silver_xyz` | `schema_conformance_pass_rate` | Partial/full parse quality |
| `index_reports` | `embedding_id_present` | All silver.reports rows have `cardinality(embedding_ids) > 0` |
| `index_reports` | `parser_error_floor` | Blocking on 0% embedded; WARN otherwise |
| `silver_drill_traces` | `desurvey_trace_count_matches_collar_count_with_surveys` | Trace count ≥ collars-with-surveys count |
| `bronze_raster_uploads` | `bronze_raster_sources_discoverable_check` | S3 list returns without error (empty OK) |
| `silver_cog_rasters` | `cog_readable_check` | All COG files in bronze-raster/cog/ are rasterio-readable |

**Silver-trapped note:** `silver_seismic` and `silver_xyz` have checks but no Gold/Index downstream. Their data commits to Silver but is not yet embedded or graph-linked. Retrieval wiring: `ops/backlog/module-4-intake.md` (XYZ) and `ops/backlog/module-8-intake.md` (seismic).

Check implementations: `src/dagster/georag_dagster/checks/silver_checks.py`, `evidence_checks.py`, `index_checks.py`, `drill_traces_checks.py`.

---

_Written 2026-04-20 during Module 3 Phase D. Update this file whenever the underlying procedure changes._
