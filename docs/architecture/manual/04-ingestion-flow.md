# Chapter 04 — Ingestion Flow

> **Reconciled 2026-09-07** against `src/fastapi/app/hatchet_workflows/`,
> `app/routers/shadow_trigger.py` and the Laravel upload path. The diagram
> and the workflow tables described a two-pool worker, a Neo4j fan-out leg,
> a Dagster path for every non-PDF format, and several workflows that are
> not registered. Six ingest workflows exist and all of them are Hatchet.

From a file appearing in the browser to a citable answer in the chat. Every
step here has a file:line anchor.

## 1. The 30-second picture

```
Browser ── multipart upload ──▶ laravel-octane
                                     │
                                     ├─ write bytes to object storage (SeaweedFS dev / S3 prod)
                                     ├─ insert bronze.ingest_runs (status=running)
                                     ├─ HatchetDispatchThrottle gate
                                     └─ POST /internal/v1/shadow/{workflow}/trigger  ──▶ fastapi
                                            ingest_pdf | ingest_tabular | ingest_spatial
                                            | ingest_well_logs | ingest_zip_archive
                                            | tiff_normalize
                                                              │
                                                              ▼
                              ┌──────────────────────────────────────────┐
                              │ hatchet-worker  (WORKER_POOL=all)        │
                              │   preflight → parse → persist            │
                              │   → embed_verify                         │
                              │   → Reverb progress events               │
                              └────────────────────┬─────────────────────┘
                                                   │
                                                   ▼
                                    embed_pending_passages (cron + inline)
                                                   │
                                                   ▼
                                    Qdrant georag_chunks (dense + sparse)
                                                   │
                                                   ▼
                                    silver.ingest_progress (status=completed)
                                                   │
                                                   └─ broadcast on workspace.{id}.activity
                                                              │
                                                              ▼
                                                   browser refetches affected views
```

**Two things this diagram used to get wrong.** There is one worker, not an
ingestion pool and an AI pool — they were merged, and `hatchet-worker-ai`
does not exist. And every format has a Hatchet workflow: the claim that
CSV/LAS/XLSX/GIS go through Dagster assets was true until 2026-07-28 and is
now the opposite of the truth. The Neo4j leg of the fan-out is gone with the
graph.

## 2. Upload endpoints (Laravel)

`POST /api/projects/{project}/uploads` — main entry for drill data uploads
(CC-01 Item 1 flow). Controller dispatches a Laravel queue job which:

1. Validates the multipart form (`PHP_UPLOAD_MAX_FILESIZE=2G`,
   `POST_MAX_SIZE=2G`, Swoole `package_max_length` and `socket_buffer_size`
   all raised in lockstep — see
   [project_upload_size_stack_2026_05_21](../notes/INDEX.md#project_upload_size_stack_2026_05_21)).
2. Streams the upload to SeaweedFS through aioboto3 (S3 endpoint
   `http://minio:8333`, `AWS_USE_PATH_STYLE_ENDPOINT=true`).
3. Writes a `bronze.ingest_runs` row with `status='running'` plus
   `bronze.ingest_manifest` entries. **`bronze.upload_files` is not a
   table** — see [Ch 14](14-status-matrix.md#key-tables).
4. Calls the FastAPI shadow ingest trigger endpoint:
   `POST /internal/v1/shadow/ingest_pdf/trigger` (Service-Key
   `FASTAPI_SERVICE_KEY` HMAC).
5. FastAPI enqueues the workflow with `aio_run_no_wait` through the Hatchet
   client (`HATCHET_CLIENT_TOKEN`, `HATCHET_CLIENT_HOST_PORT`).
   `HatchetDispatchThrottle` on the Laravel side rate-limits uploads before
   they reach this point ([Ch 07 §2.3](07-orchestration.md)).

The upload cap is `GEORAG_MAX_UPLOAD_BYTES`, 512 MB.

QField uploads land via a separate sub_type=218 route documented in
[project_cc03_item4_qfield_ingestion](../notes/INDEX.md#project_cc03_item4_qfield_ingestion).

## 3. The ingest_pdf Hatchet workflow

[src/fastapi/app/hatchet_workflows/ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py).
Decomposed into 5 steps + 1 on-failure task:

| Step | Function | What it does | Side-effects |
|------|---------|---|---|
| `preflight` | `preflight()` | S3 GET, magic-byte check, sha256, page count, size cap | Updates `silver.ingest_progress` to `preflight_complete` |
| `parse` | `parse()` → `_parse_body()` → `_run_parser_subprocess()` | Runs the §04p PDF stack (see [Ch 05](05-pdf-stack.md)) in a memory-guarded subprocess pool | Caches the body bytes under `BRONZE_LOCAL_DIR` |
| `persist` | `persist()` → `_persist_body()` | Writes `silver.reports`, `silver.report_pages`, table extractions, figure captions; inserts into `bronze.provenance`; bumps `silver.workspaces.data_version` | Bronze provenance trigger auto-fills `workspace_id` |
| `embed_verify` | `embed_verify()` | Inserts a verify task into Hatchet for the `ai` pool to pick up; gates the run on the embed sweep finishing | Posts a Reverb progress event |
| `p04p_dual_write` | `p04p_dual_write()` | When `P04P_DUAL_WRITE_ENABLED=true`, also runs the legacy parser and diffs (shadow A/B) | Writes to `silver.shadow_runs` |
| `on_failure` | `on_failure_task` (line 1428) | Marks the run failed, broadcasts a failure Reverb event | Hatchet retries this task itself (retries=2) |

Memory protection:
- `_compute_parse_max_workers()` returns `min(os.cpu_count(), 4)` when
  `PARSE_SUBPROCESS_MAX_WORKERS` is empty.
- `_wait_for_memory_headroom()` awaits `psutil.virtual_memory().available ≥ MIN_FREE_RAM_MB`,
  raising `MemoryError` after `MEMORY_WAIT_MAX_S` so Hatchet retries on a
  freer worker
  ([docker-compose.yml:2055-2065](../../../docker-compose.yml)).

## 4. The other ingest workflows

Six workflows accept uploads. The full 51-workflow registry, with every
cron, lives in [Ch 07 §2.2](07-orchestration.md); this table is the ingest
subset.

| Workflow | Module | Accepts |
|---|---|---|
| `ingest_pdf` | `ingest_pdf.py` | drill reports, NI 43-101 PDFs |
| `ingest_tabular` | `ingest_tabular.py` | CSV, XLSX, Access MDB, dBASE, MapInfo DAT |
| `ingest_spatial` | `ingest_spatial.py` | shapefile, GeoJSON, GPKG, QGIS projects, XYZ, DC/IP, Surpac |
| `ingest_well_logs` | `ingest_well_logs.py` | LAS |
| `ingest_zip_archive` | `ingest_zip_archive.py` | ZIP fan-out to the above, with a parent run row in `silver.archive_ingest_runs` |
| `tiff_normalize` | `tiff_normalize.py` | multi-page TIFF → PDF, then dispatches `ingest_pdf` in-process (ADR-0005) |

Downstream of the parse:

| Workflow | Role |
|---|---|
| `embed_pending_passages` | embeds `silver.document_passages` rows and upserts to `georag_chunks`. Dispatched inline by `ingest_pdf` and on crons `45 5 * * *` and `*/10 * * * *`. Also runs the Qdrant-vs-Postgres count check that emits `QDRANT_PARTIAL_LOSS` |
| `enrich_passage_context` | contextual-retrieval headers |
| `nl_summaries` | structured rows → natural-language passages (ADR-0012) |
| `verbalize_page_images` | page-image descriptions; inert unless `IMAGE_VERBALIZATION_ENABLED` |
| `promote_silver_to_gold` | silver → the gold visual tables the Workspace reads |
| `stale_run_detector` | closes `silver.ingest_progress` rows with no heartbeat, but only after Hatchet confirms the run is no longer QUEUED/RUNNING, so a queued bulk upload is not timed out (2026-09-02) |
| `nightly_ingestion_integrity` | cross-checks bronze against silver |

**Not registered**, despite appearing in earlier versions of this chapter:
`re_ocr_page`, `ocr_quality_check_wf`, `tiff_ocr_cluster`,
`sync_silver_to_kg`, `shadow_diff`, `evaluate_workspace`,
`eval_real_rag_nightly`, `score_answer_quality`, and the five `backup_*`
workflows. OCR quality is decided inside `ingest_pdf`; the backup workflows
were deleted 2026-08-23 in favour of the managed provider's PITR ([Ch 02 §8](02-data-stores.md)).

## 5. Non-PDF ingestion

Non-PDF formats go through the Hatchet workflows above, parsing via the
[`georag_geoparsers`](../../../src/georag_geoparsers/georag_geoparsers/)
package ([Ch 05 §11](05-pdf-stack.md#11-non-pdf-parsers)). The Dagster asset
graph that used to own this — roughly forty `bronze_*`, `silver_*`,
`gold_*`, `silver_to_gold/*` and `index_*` assets — was retired on
2026-07-28 and deleted on 2026-08-28.

Two consequences worth stating plainly:

- **The gold visual tables had no writer for a month.** `silver_drill_traces`,
  `gold_cross_section_panels`, `gold_drillhole_intervals_visual` and
  `gold_structure_measurements_visual` were Dagster assets. Measured against
  the then-live production database on 2026-08-25, every one of those tables held zero
  rows beside cleanly ingested collars and surveys. `promote_silver_to_gold`
  (2026-08-25) restored the step. A project ingested between those dates
  needs that workflow run against it.
- **SEG-Y and Word ingestion are gone**, not moved. `segy_parser.py` and
  `docx_parser.py` went with the tree and `segyio`/`obspy` are not
  dependencies.

`silver.entities` was written by `silver_entity_ner_backfill`; entity rows
now live in `workspace.entities` ([Ch 14](14-status-matrix.md#key-tables)).

## 6. The outbox pattern

A silver write is atomic with an `outbox.pending_propagations` insert in the
same transaction. The Hatchet `outbox_dispatcher` workflow polls
`SELECT … FOR UPDATE SKIP LOCKED` every minute and fans out to:

- **Qdrant** — for newly embedded passages
- **SeaweedFS / Blob** — for any object payload mirror
- ~~**Neo4j**~~ — the dispatcher still has a `_dispatch_neo4j` branch, and it
  returns `transient_failure` for a store that no longer exists, so a stray
  row burns three attempts before dead-lettering ([Ch 07 §2.5](07-orchestration.md))

Each attempt is recorded in `outbox.propagation_attempts`. After 3 transient
failures the row is dead-lettered. Idempotency is per-target — re-dispatch is
safe.

**As built (2026-09-07): the ingest path does not use the outbox.** The only
two writers of `outbox.pending_propagations` are Phase 0 agents. The atomic
silver-write-plus-outbox insert described above is the design; the ingest
workflows write silver and dispatch embedding directly.

## 7. Embed dispatch race + the verify-task fix

[project_pipeline_resilience_2026_05_22](../notes/INDEX.md#project_pipeline_resilience_2026_05_22):
- The `embed_verify` step in `ingest_pdf` schedules a verify task that
  re-checks the embed sweep status.
- A 10-min cron sweep (`embed_pending_passages_wf` heartbeat) catches any
  rows the inline trigger missed if a worker crashed mid-flight.

## 8. Completion-write fix

[project_ingest_completion_terminal_2026_05_25](../notes/INDEX.md#project_ingest_completion_terminal_2026_05_25):
- Embed sweep now writes `status='completed'`.
- `stale_run_sweep` recovers any stuck runs.
- Tests must reuse the state-machine-tests workspace under RLS.

## 9. Reverb broadcast surface (post-ingest)

| Channel | Event | Listened by |
|---|---|---|
| `project.{projectId}.ingestion` | `IngestionProgressBroadcast` | `IngestionRuns.tsx` |
| `workspace.{workspaceId}.activity` | `WorkspaceActivityBroadcast`, `WorkspaceDataUpdated` | pages that refetch on data change, via `useWorkspaceActivity` / `useWorkspaceDataUpdated` |
| `query.{queryId}` | `QueryStreamEvent` | `Chat.tsx` |

`laravel_bridge.post_workspace_data_updated()` in
[`services/laravel_bridge.py`](../../../src/fastapi/app/services/laravel_bridge.py)
is the helper the FastAPI and Hatchet sides call — it POSTs to
`http://laravel-octane/api/internal/v1/broadcast/...` with the shared
`FASTAPI_SERVICE_KEY`. There is no `commit_ingestion_run` step any more;
that was a Dagster asset. See [Ch 10 §3](10-frontend.md) for the full
channel list, including the 22 `admin.*` channels nothing subscribes to.
