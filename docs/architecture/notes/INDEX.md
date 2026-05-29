# Architecture Notes — Incident & Decision Index

This file replaces the per-developer memory paths previously cited inline
(`C:/Users/GeoRAG/.claude/projects/C--Users-GeoRAG/memory/...`) with a
checked-in, repo-relative index. Each entry below is a one-line precis of
the original memory note. Promote any entry to an ADR under
[docs/adr/](../../adr/) or an incident note under
[docs/architecture/incidents/](incidents/) when it acquires durable consequence.

> **Status:** This is a *bridge document*. Long-term plan is to migrate each
> note that still carries decision authority into a numbered ADR. Notes
> marked **promote** below are the priority candidates.

---

<a id="project_init_roles_gap"></a>
### project_init_roles_gap — **promote**
`docker/postgresql/init-roles.sql` lives *outside* the auto-init directory
(`/docker-entrypoint-initdb.d/`). On a fresh cluster the `georag_read /
_write / _audit` group roles do not exist until the script is applied
manually. Fix: move to `init/` and renumber so it runs after
`init-postgis.sql`.

<a id="project_gpu_acceleration_2026_05_22"></a>
### project_gpu_acceleration_2026_05_22
A4500 wired to `hatchet-worker-ai`. bge-small CPU 3-4 chunks/s → GPU 144
chunks/s. Requires `VLLM_GPU_MEM_UTIL ≤ 0.80` to leave ~1.6 GB VRAM headroom.

<a id="project_upload_size_stack_2026_05_21"></a>
### project_upload_size_stack_2026_05_21
Four independent upload caps (Swoole `package_max_length`,
`socket_buffer_size`, PHP `upload_max_filesize`/`post_max_size`, Laravel
validator) must all rise in lockstep. Current: 2 GB.

<a id="project_cc03_item4_qfield_ingestion"></a>
### project_cc03_item4_qfield_ingestion
QField GPKG ingestion landed 2026-05-24. Sub-type id 213 collision → used
218. GPKG non-spatial sidecar layer filter. WSL bind-mount stale-tree
workaround.

<a id="project_pipeline_resilience_2026_05_22"></a>
### project_pipeline_resilience_2026_05_22
Two edge cases from overnight run: heartbeat starvation during heavy parses
(subprocess pool fix) + embed-dispatch race after Hatchet retries (verify
task + 10-min cron sweep).

<a id="project_ingest_completion_terminal_2026_05_25"></a>
### project_ingest_completion_terminal_2026_05_25 — **promote**
Embed sweep was missing the terminal `status='completed'` write. Added
`stale_run_sweep` recovery dispatch. Tests must reuse the state-machine
workspace under RLS.

<a id="project_parse_perf_2026_05_22"></a>
### project_parse_perf_2026_05_22
Eight perf fixes (PyMuPDF primary, parallel pdfplumber, diverse table probe,
OCR-skip probe, PDF body cache, single-pass tables, slot tuning, docling on
GPU) → 3-5× speedup on text-heavy NI 43-101s.

<a id="project_overnight_run_2026_05_22"></a>
### project_overnight_run_2026_05_22
Five high-impact extraction fixes (psm=3, all-page tables, §04p re-enable,
docling opt-in primary, figure→caption linking v1 + MinIO upload) and the
outstanding limitations table.

<a id="project_pdf_coverage_overhaul_2026_05_22"></a>
### project_pdf_coverage_overhaul_2026_05_22
Six PDF gaps closed: per-page OCR, page_first/page_last tracking, atomic
persist, inline embed trigger, www-data cache env vars, docker-commit-CMD
gotcha.

<a id="project_pdfminer_loglevel_hatchet_block_2026_05_21"></a>
### project_pdfminer_loglevel_hatchet_block_2026_05_21
`LOG_LEVEL=debug` + pdfminer flood blocked the Hatchet asyncio loop →
ingest_pdf steps cancelled → `silver.reports` stayed empty. Fix: pin
pdfminer’s logger to INFO regardless of global level.

<a id="project_tiff_smoke_2026_05_23"></a>
### project_tiff_smoke_2026_05_23
ADR-0005 TIFF normalise verified end-to-end (3.1 s). Smoke exposed
pre-existing §04p parse subprocess-pool instability on image-only PDFs.

<a id="project_csv_audit_2026_05_23"></a>
### project_csv_audit_2026_05_23
Three real CSV gaps fixed: delimiter auto-detect, decimal-comma transform,
Dagster concurrency pool. 15 new tests, 148 existing CSV tests still green.

<a id="project_xlsx_audit_2026_05_23"></a>
### project_xlsx_audit_2026_05_23
Fixed silent data loss on multi-sheet workbooks (`sheet_type=''` now
auto-dispatches). Sheet-type classifier reuses CSV aliases. `silver_xlsx`
joins the `csv_silver_ingest` pool.

<a id="project_workspace_3d_expansion_2026_05_25"></a>
### project_workspace_3d_expansion_2026_05_25
Foundry Workspace 3D went 1 → 9 sub-views. Surfaced three nontrivial bugs
(silver.structures plural, surveys-from-curves fallback, structure
measurements schema drift). Cameco gotcha: U₃O₈ lives in `silver.samples`,
not `gold.assay_composites`.

<a id="project_test_db_parity_gap"></a>
### project_test_db_parity_gap
Convention for 120 migrations that touch raw-SQL tables: add a
`*_provision_*_for_test_db.php` sibling mirror, not `to_regclass` guards or
full phase0 import. Chain clean as of 2026-05-21.

<a id="project_test_db_parity_and_policy_normalization_2026_05_25"></a>
### project_test_db_parity_and_policy_normalization_2026_05_25
Closed last 2 items: provisioned silver `workspace_id` into test DB (0
skips), normalised 41/61 layered RLS policies. Remaining 20 use
`IS DISTINCT FROM` and are semantically distinct.

<a id="project_agentic_dispatcher_ctx_fix_2026_05_25"></a>
### project_agentic_dispatcher_ctx_fix_2026_05_25 — **promote**
`_call_tool_safely` was silently dropping all 6 legacy intents' tool results
(missing `RunContext` arg). Fixed + introspection regression test pins to
real signatures.

<a id="project_hole_id_extraction_2026_05_21"></a>
### project_hole_id_extraction_2026_05_21
`extract_hole_ids` only matched letter-prefixed IDs (PLS-22-08); `downhole`
gate required lithology keywords. "tell me about hole 36-1085" (Cameco
numeric format) refused. Fixed both.

<a id="project_chat_cards_initiative_2026_05_25"></a>
### project_chat_cards_initiative_2026_05_25 — **ADR-0007 (Proposed)**
4-PR plan for `project_summary` / `coverage_gap` intents + 5 inline card
types. Schema ready, extractors partially missing.

<a id="project_bsg_buildout_2026_05_22"></a>
### project_bsg_buildout_2026_05_22
A1-A5 + B6-B7 shipped: Lakehouse + DrillholeDetail Inertia pages; B8/B9
deferred; migration + npm build not yet run.

<a id="project_latency_fix_2026_05_20"></a>
### project_latency_fix_2026_05_20
bge-reranker-base CPU was blowing the 2s `search_documents` `wait_for`.
Fixed via split timeouts (Qdrant 2s / reranker 8s) +
`torch.set_num_threads(10)` + pre-truncate to 2000 chars + halve candidates
to 10. Cold path now returns real answers (~6s) instead of fast refusals
(~3s).

<a id="project_reranker_v1"></a>
### project_reranker_v1
Fine-tune bge-reranker-base in place (path c). Synthetic-label asset + eval
harness landed 2026-05-19. LoRA training next.

<a id="project_reverb_dual_purpose_env_2026_05_21"></a>
### project_reverb_dual_purpose_env_2026_05_21
`REVERB_HOST/PORT` serve two purposes; Vite doesn't expand `${VAR}`;
shipped literal `${REVERB_HOST_PORT}` in bundle → wsPort defaulted to 80
→ 60s "channel dropped" timeouts. Fixed: server uses `laravel-reverb:8080`,
browser uses literal `8085`.

<a id="feedback_octane_vite_reload"></a>
### feedback_octane_vite_reload
After every `vite build`, run `octane:reload` — workers cache the Vite
manifest and 404 on stale bundle hashes otherwise.

<a id="project_bronze_tenancy_rls_2026_05_25"></a>
### project_bronze_tenancy_rls_2026_05_25 — **promote**
Closed Lakehouse cross-WS leak: RLS on all 3 bronze tables, nullable
`workspace_id` added to `ingest_manifest` + `provenance`, per-table scope
pills surfaced in UI.

<a id="project_rls_coverage_audit_2026_05_25"></a>
### project_rls_coverage_audit_2026_05_25 — **promote**
Closed 14 more tenancy leaks from the May 20-25 build-out wave + added a
durable regression test (`WorkspaceRlsCoverageTest`).

<a id="project_parked_items_2026_05_25"></a>
### project_parked_items_2026_05_25
Closed 4 deferred items: 12 always-fail-open RLS policies (broken GUC),
bronze `workspace_id NOT NULL`, dagster_metrics config gap, Compare /
IngestionRuns 500 fixes.

<a id="project_sentry_removed_2026_05_21"></a>
### project_sentry_removed_2026_05_21
`sentry/sentry-laravel` is NOT installed. `.env` wiring is commented out.
Re-enabling requires `composer require` + worker restarts, not just env
flips.
