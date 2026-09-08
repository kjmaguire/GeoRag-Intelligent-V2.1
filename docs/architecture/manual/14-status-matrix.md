# Chapter 14 — Status Matrix

> **Reconciled 2026-09-07** against `docker-compose.yml`, the Azure manifests
> under `deploy/azure/`, `src/fastapi/app/hatchet_workflows/worker.py`,
> `src/fastapi/app/config.py` and the migration tree.
>
> **Production column re-reconciled 2026-09-08** against
> `deploy/aws/terraform/` and `src/fastapi/app/config.py` after the move to
> AWS ([ADR-0022](../../adr/0022-aws-replaces-azure-as-the-production-cloud.md)).
> Unlike the narrative chapters, this one is a lookup table — someone reads
> a single row and acts on it — so the production column and the flag
> defaults were rewritten rather than left under a dated notice. The service table used to
> list Neo4j, Dagster, Kestra, Caddy, vLLM, Prometheus, Grafana, Loki, Tempo,
> the exporters, Ofelia and the backup agent as **Live**; all of them were
> deleted between 2026-07-28 and 2026-08-23. Rows below are what exists today.
> Table, page and agent rows outside the service and workflow lists were spot
> checked, not re-verified one by one — treat a surprising one as a question,
> not a fact.

A single place to look up "is this thing real today?" for every component
the rest of the manual mentions.

## Status legend

| Marker | Meaning |
|---|---|
| **Live** | In production-ready state on the current main branch. Verified by tests + manual smoke. |
| **Live (dev-only)** | Wired and used on the dev workstation; not yet hardened for prod. |
| **Partial** | Shipping but with explicit gaps documented in the relevant chapter. |
| **Planned** | Documented + designed; not yet implemented. |
| **Deprecated** | Was live; replaced by something else; still present in the tree for rollback. |
| **Stub** | Function/table/view exists but raises or returns empty. |
| **Experimental** | Behind a feature flag, opt-in. |

## Services

Sixteen compose services and ten ECS services. Profiles and images are in
[Ch 01](01-services.md); this table is the status view. Production is
[`deploy/aws/terraform/services.tf`](../../../deploy/aws/terraform/services.tf)
— if a thing is not there, it does not exist in production.

| Service | Dev (compose) | Production (ECS) | Notes |
|---|---|---|---|
| `postgresql` | Live | RDS `georag-pg` (PG18, Single-AZ) | dev image `georag/postgres:18-ext`; RDS is managed, so no PgBouncer. Extensions need `deploy/aws/bootstrap.sql` applied once by hand |
| `pgbouncer` | Live | not deployed | transaction pooling; Martin, Hatchet and migrations bypass it |
| `redis` | Live | `redis` (task, EFS at `/data`) | AOF on with `--save ""`, `volatile-lru`. The Azure app had AOF on with **no volume**; ADR-0022 fixed it |
| `martin` | Live | `martin` | dev connects as `georag_app`; `martin_readonly` is still the role to use and still not used |
| `laravel-octane` | Live | `laravel-octane` (2 tasks) | the ALB's default target. Two tasks so a deploy is not an outage — it ran at 1 on Azure |
| `laravel-horizon` | Live | `laravel-horizon` | two supervisors, three jobs ([Ch 07 §1](07-orchestration.md)) |
| `laravel-reverb` | Live | `laravel-reverb` | 60 s channel-drop bug fixed 2026-05-21. The **second** ALB-reachable service — a listener rule routes to it — with target-group stickiness on, because a WebSocket lives on one task for its whole life |
| `fastapi` | Live | `fastapi` | internal only; reached over Cloud Map service discovery, never the ALB |
| `reranker` | Live (dev-only) | not deployed | Qwen3-Reranker-0.6B on the one GPU; production uses Cohere Rerank **3.5** on Bedrock (v4 is not offered — see §"Known" below) |
| `embedding` | Live (dev-only) | not deployed | Qwen3-Embedding-0.6B on CPU; production uses Cohere Embed v4 on Bedrock at 1024-dim |
| `sparse` | Live (dev-only) | **`sparse` (task)** | SPLADE++; **no managed equivalent on any cloud**, so it is self-hosted on Fargate or the sparse leg of hybrid retrieval does not exist. ADR-0022 chose self-hosted |
| `qdrant` | Live | `qdrant` (task, EFS) | auth off in dev by design (Ch 02 §2). Azure had an Azure Files share with a fixed quota and a key-based mount; EFS is elastic and IAM-authorised |
| `minio` (SeaweedFS) | Live | not deployed | production uses S3 via `STORAGE_BACKEND=s3_compatible` with endpoint and credentials unset, so boto3 resolves the region endpoint and the task role |
| `minio-init` | Live | not deployed | one-shot bucket creation |
| `hatchet-lite` | Live | `hatchet` | dev `v0.86.12` — check the deployed tag in `config.tf` rather than assuming parity |
| `hatchet-worker` | Live | `hatchet-worker` | one merged worker, `WORKER_POOL=all`, 51 workflows. Unlike Azure it genuinely stops overnight (`desired-count 0`) |
| `langfuse-web` / `-worker` / `clickhouse` | Opt-in overlay | not deployed | `docker/compose.langfuse.yml`; not in the default `up` |

Two more exist in production only, as EventBridge-scheduled RunTasks rather
than services: `georag-shutdown-sweep` and `georag-startup-sweep`
([Ch 07 §3](07-orchestration.md)).

### Removed

| Service | Removed | Replaced by |
|---|---|---|
| `neo4j` (+ warmup), `neo4j_exporter` | 2026-07-28 | nothing — the graph was dropped |
| `dagster-daemon` / `dagster-webserver` | 2026-07-28 (tree deleted 2026-08-28) | Hatchet workflows |
| `kestra`, `caddy` | 2026-07-28 | nothing |
| `prometheus`, `alertmanager`, `grafana`, `loki`, `promtail`, `tempo`, `otel-collector`, `redis_exporter`, `postgres_exporter` | 2026-07-28 | Azure Monitor, then CloudWatch ([Ch 12](12-observability.md)) |
| `vllm` (+ warmup) | 2026-07-30 | Azure AI Foundry, then Amazon Bedrock (ADR-0022) — Cohere Command A+ either way. `LLM_BACKEND=vllm` remains a supported value for an operator running their own OpenAI-compatible endpoint |
| `ofelia`, `backup-agent` | 2026-08-19 / 08-23 | RDS automated backups (35-day PITR); see the backup gap in [Ch 02 §8](02-data-stores.md) |
| `ollama` | 2026-05-17 | — |
| `activepieces` | Phase 3 Step 7 | Kestra, itself since removed |
| `georag-phase-e-ocr` (TIFF bulk OCR) | ADR-0005 | `tiff_normalize` |

## Postgres schemas

| Schema | Status |
|---|---|
| `bronze`, `silver`, `gold`, `public`, `audit`, `usage`, `outbox`, `workflow`, `workspace`, `partman`, `interpretation`, `targeting`, `ops`, `eval`, `topology`, `backups` | Live |
| `public_geo` | Live; rename to `public_geoscience` planned (see [martin.yaml:5](../../../docker/martin/martin.yaml)) |
| `silver.data_categories`, `silver.dataset_categories` | **Planned** (Ch 13) |

## Key tables

| Table | Status |
|---|---|
| `silver.workspaces`, `silver.projects`, `silver.collars`, `silver.assays_v2`, `silver.lithology`, `silver.samples`, `silver.reports`, `silver.drill_traces` | Live |
| `silver.review_queue` | Live (added 2026-05-24) |
| `silver.ingest_progress` | Partial (Phase A live; Phase B writes from Hatchet steps planned) |
| `silver.geophysics_surveys` | Live (added 2026-05-21) |
| `silver.query_traces` | Live (2026-05-26, extended 2026-05-28) — see [Ch 16 §4](16-algorithmic-spines.md) |
| `silver.data_quality_flags` | Live schema; 4 DQ writers live; rule engine partial |
| `silver.document_versions` | Live (2026-05-26) — closes document_versioning_design |
| `silver.entity_aliases`, `silver.entity_gaps` | Live (2026-05-26) — backs Spine A entity_resolver |
| `gold.repair_shadow_daily` | Partial — `repair_shadow_aggregate` writes rows; nothing reads them — the dashboard was to be Grafana, which is gone |
| `silver.tenant_isolation_audit` | Live (2026-05-30) — Z.9 nightly verifier run log; RLS off (admin-gated); see [Ch 18 §8](18-model-stack-evolution.md) |
| `silver.archive_ingest_runs` | Live (2026-06-03) — ZIP-archive upload parent row; RLS-scoped; closes `ingest_zip_archive` silent-failure gap |
| `silver.projects.lifecycle_state` | Live (2026-05-30) — **CC-03 Item 8 LANDED** (was deferred); active/hibernated/archived/past_due; billing still unbuilt; [Ch 18 §7](18-model-stack-evolution.md) |
| `silver.document_passages.contextualized_content` | Live (2026-05-30) — Anthropic contextual-retrieval header; written by `enrich_passage_context` workflow |
| `audit.query_audit_log` quality cols | **Columns exist, no writer since 2026-07-27.** `faithfulness_score` + `context_precision_score` are REAL columns on a live Eloquent model, but their only producer (`score_answer_quality.py`) was Removed (09d1d35, 2026-07-27). Every row is NULL and will stay NULL. A `WHERE faithfulness_score < x` filter returns zero rows, which reads as 'no low-faithfulness answers' rather than 'nothing is scored'. |
| `silver.answer_runs`, `silver.answer_citation_items`, `silver.answer_citation_spans`, `silver.message_feedback`, `silver.evidence_items`, `silver.document_passages` | Live |
| `silver.hypotheses`, `silver.decision_records` (+ children) | Live |
| `silver.report_pages`, `silver.report_figures`, `silver.report_tables` | Partial — created by `ingest_pdf.persist` but column set still drifts; see appendix A |
| `silver.entities` | **Not present.** Entity rows live in `workspace.entities`; references to `silver.entities` in older docs are wrong. Tracked in appendix Z. |
| `silver.lithology_intervals` | **Not present.** The canonical table is `silver.lithology` (new) coexisting with the legacy `silver.lithology_logs`. Tracked in appendix Z. |
| `bronze.provenance` | Live; auto-fill trigger 2026-05-25 |
| `bronze.ingest_runs`, `bronze.ingest_manifest`, `bronze.ingest_triage_samples` | Live |
| `bronze.upload_files` | **Mentioned in this manual but not yet a created table.** Currently the upload flow writes only to `bronze.ingest_runs` + `bronze.ingest_manifest` + the SeaweedFS `bronze` bucket. Tracked in appendix Z; either create `bronze.upload_files` or rename existing manual references. |
| `bronze.raw_samples` | **Planned.** Drillhole bronze tables landed in [2026_05_20_060000](../../../database/migrations/2026_05_20_060000_create_bronze_drillhole_tables.php) — assay / lithology / surveys / geophysical / collars; no `raw_samples` table yet. |
| `bronze.manifest` vs `bronze.ingest_manifest` | Both exist. `bronze.ingest_manifest` is the canonical per-file manifest inside an ingest run (Phase A). `bronze.manifest` is a newer table from [2026_05_25_020540](../../../database/migrations/2026_05_25_020540_create_bronze_manifest.php) used by the May 25 ingest UI track. Rename/consolidate tracked in appendix Z. |
| `audit.audit_ledger` (+ verification_runs, fork_quarantine, query_audit_log) | Live |
| `gold.h3_density_mineral`, `gold.cross_section_panels`, `gold.drillhole_intervals_visual`, `gold.structure_measurements_visual`, `gold.mv_refresh_log` | Live |
| `gold.significant_intersections` | **Live** — persisted table from [2026_05_20_060700](../../../database/migrations/2026_05_20_060700_create_gold_drillhole_tables.php); `promote_silver_to_gold` upserts it (Dagster's `silver_to_gold/significant_intersections` did until 2026-07-28). The Martin function [2026_05_20_061000](../../../database/migrations/2026_05_20_061000_create_martin_significant_intersections_function.php) reads from it. |
| `gold.drill_summaries`, `gold.zone_statistics`, `gold.qaqc_statistics`, `gold.campaign_summaries`, `gold.element_correlations` | Live — created in the same [2026_05_20_060700](../../../database/migrations/2026_05_20_060700_create_gold_drillhole_tables.php) batch; written by `promote_silver_to_gold` |
| `public.smdi_deposits` | Live (6,012 SK deposits) |
| `public_geo.pg_*` + `v_pg_*_mvt` | Live (Tier 1); Tier 2/3 sources commented out in [martin.yaml](../../../docker/martin/martin.yaml). The phase-0 verification counts a `public_geoscience` namespace that does not exist, so it always reports 7/8 ([Ch 02 §1.5](02-data-stores.md)) |

## Hatchet workflows

| Workflow | Status |
|---|---|
| `ingest_pdf` | Live |
| `embed_pending_passages_wf` | Live |
| `outbox_dispatcher` | Live |
| `audit_ledger_verify` | Live |
| `stale_run_detector`, `nightly_ingestion_integrity`, `reliability_metrics_publisher` | Live |
| `re_ocr_page`, `ocr_quality_check_wf` | **Not registered.** Neither name appears in `worker.py`; OCR quality is decided inside `ingest_pdf` ([Ch 05](05-pdf-stack.md)) |
| `mv_refresh_silver` | Live |
| `sync_silver_to_kg` | **Deleted** 2026-07-28 with Neo4j — the silver→graph sync has no target |
| `score_targets` | Live |
| `external_notification` | Live, but **no caller** — Kestra was its only trigger and is gone |
| `public_geoscience_pull`, `public_geo_sync` | Live as workflows; the Kestra flow that drove the first is gone, so it now runs on its own cron or by hand |
| `backup_postgres / backup_neo4j / backup_qdrant / backup_redis / backup_seaweedfs` | **Deleted** — `backup_neo4j` 2026-08-19 (Neo4j dropped in B1), the other four 2026-08-23. All wrote to a SeaweedFS substrate that does not exist on Azure, so every run had failed since the migration. Deliberate: Postgres carries 35-day PITR from the managed provider's automated backups, Qdrant is rebuildable by re-embedding from `silver.document_passages`, and Redis is cache plus Horizon queues. Object storage was the one irreplaceable copy with no backup at all on Azure; on S3 it has versioning and 90-day non-current retention (ADR-0022). Nothing has been restore-tested. |
| `cold_tier_archive_workflow` | Partial — bucket policies live, lifecycle automation tested only on small sets |
| `workspace_export`, `restore_workspace` | Partial — golden path verified, larger-than-RAM workspaces unproven |
| `evaluate_workspace`, `eval_real_rag_nightly` | Removed (09d1d35, 2026-07-27). The scheduled RAG evaluation no longer exists; the only surviving entry point is the operator CLI `src/fastapi/scripts/run_golden_benchmark.py`, plus the `eval-gate.yml` nightly harness self-check (stubbed LLM — it proves the harness imports, not that answers are good). |
| `continuous_learning_loop`, `field_outcome_learning` | Experimental |
| `tiff_ocr_cluster` | Deprecated (replaced by `tiff_normalize` per ADR-0005) |
| `repair_shadow_aggregate` | Live (added 2026-05-27 per ADR-0009 / [Ch 16 §2](16-algorithmic-spines.md)) — cron `15 2 * * *` UTC |
| `enrich_passage_context` | Live (2026-05-30) — contextual-retrieval header generation; daily 04:30 UTC; [Ch 18 §5](18-model-stack-evolution.md) |
| `score_answer_quality` | Removed (09d1d35, 2026-07-27) — the LLM-as-judge faithfulness + context-precision scorer no longer exists. Nothing in production measures answer quality. |
| `ingest_zip_archive` | Live (2026-06-03) — ZIP fan-out with parent-run observability; [Ch 18 §8](18-model-stack-evolution.md) |
| `tiff_normalize` | Live (ADR-0005 — normalises TIFFs to PDF then routes through `ingest_pdf`) |
| `train_source_trust` | Experimental (writes `silver.source_trust_scores`) |
| `train_target_model` | Experimental (target-scoring model refresh) |
| `what_changed_detector`, `what_changed_weekly` | Live (drives the WhatChangedFeed page) |
| `phase2_smoke`, `phase0_agents`, `support_replay`, `cost_burn_watcher`, `generate_report`, `flow_jwt_key_reaper`, `idempotency_keys_cleanup` | Live |
| `shadow_diff`, `shadow_diff_scan` | **Removed** — see the note at `worker.py:93` |
| `answer_quality_watch`, `qdrant_payload_audit`, `retention_sweep`, `pg_partman_maintenance`, `promote_silver_to_gold`, `nl_summaries`, `verbalize_page_images`, `enrich_passage_context` | Live |
| `promote_silver_to_gold` | Live (2026-08-25) — restores the silver→gold step Dagster used to own; without it every downhole view renders empty |

## Dagster assets — deleted

Dagster was retired on 2026-07-28 and `src/dagster/` was deleted on
2026-08-28. Every asset this section used to list is gone. What replaced
each family:

| Former asset group | Today |
|---|---|
| `bronze_*` loaders | the four `ingest_*` Hatchet workflows, parsing through `georag_geoparsers` |
| `silver_*` canonicalisers | the same ingest workflows, writing silver directly |
| `gold_h3_density`, `gold_cross_section_panels`, `gold_drillhole_intervals_visual`, `gold_structure_measurements_visual` | `promote_silver_to_gold` (added 2026-08-25). Between 2026-07-28 and that date **nothing wrote the gold visual tables**, so every downhole view was empty on a freshly ingested project |
| `index_neo4j` | nothing — the graph was dropped |
| `index_document_passages`, `index_reports`, `index_public_geoscience` | `embed_pending_passages` |
| `reranker_labels` (+ helpers) | nothing — the synthetic-label pipeline went with the tree |
| `commit_ingestion_run` | the ingest workflows' own commit step |

## LangGraph nodes / RAG path

| Node | Status |
|---|---|
| `resolve_node`, `classify_node`, `route_node`, `execute_node`, `assemble_node`, `validate_node`, `demote_node`, `repair_shadow_node` | Live (behind `AGENTIC_RETRIEVAL_V2_ENABLED`) |
| `persist_node` | Partial — best-effort today; see [Ch 06 §2.1](06-retrieval-and-agents.md#21-persistence-is-currently-best-effort--fix-required) |
| Hallucination guards | **Four of six** run in `orchestrator_validators.py` (typed output, numbers, entities, constraints, plus an advisory completeness check). The retrieval-quality gate is a flat reranker-score floor and provenance is enrichment rather than a gate ([Ch 06](06-retrieval-and-agents.md), CLAUDE.md hard rule 5) |
| OIUR parser | Live (behind `GEO_ANSWER_OIUR_ENABLED`) |
| Context envelope (Field/Office mode) | Live |
| Intent classifier (8 intents) | Live; `project_summary`/`coverage_gap` extractors **partial** (ADR-0007 PR-2) |
| Inline chat cards | Partial — schema ready; 5 card extractors partially shipped |

## Frontend pages

**This list was wrong by a wide margin and is now read from the code.**
`resources/js/Pages/` holds sixteen pages, and `Inertia::render` is called
with exactly those sixteen names. Pages the previous version listed as Live
— Lakehouse, DrillReview, HoleCompare, Investigations, Hypothesis,
Decisions, Targets, SourceGraph, AuditLog, SupportCockpit, Settings,
WhatChangedFeed, Inbox, RetrievalInspector, the five dashboards and the
rest — **do not exist as pages**. Some of their functionality lives in
components under `resources/js/Components/` (79 `.tsx` files in total);
most is design-only.

| Page | File | Status |
|---|---|---|
| Login, ForgotPassword, ResetPassword | `Pages/*.tsx` | Live |
| Projects | `Foundry/Projects.tsx` | Live |
| NewProject | `Foundry/NewProject.tsx` | Live |
| Overview | `Foundry/Overview.tsx` | Live |
| Workspace | `Foundry/Workspace.tsx` | Live — the multi-mode 3D/section/log surface |
| DrillholeDetail | `Foundry/DrillholeDetail.tsx` | Live |
| Chat | `Foundry/Chat.tsx` | Live |
| Sources | `Foundry/Sources.tsx` | Live |
| Reports | `Foundry/Reports.tsx` | Live |
| IngestionRuns | `Foundry/IngestionRuns.tsx` | Live |
| DataImportWizard | `Foundry/DataImportWizard.tsx` | Live |
| AttributeTables | `Foundry/AttributeTables.tsx` | Live |
| PublicGeoscience | `Foundry/PublicGeoscience.tsx` | Live |
| RasterLayers | `Foundry/RasterLayers.tsx` | Live |
| Error | `Pages/Error.tsx` | Live (error boundary, not a route) |

See [Ch 10](10-frontend.md) for what each renders.

## Agents

Phase 0 agents are the modules in
[`src/fastapi/app/agents/phase0/`](../../../src/fastapi/app/agents/phase0/),
dispatched by the `phase0_agents` workflow.

| Agent | Module | Status |
|---|---|---|
| Index Health | `index_health.py` | Live |
| Storage Tiering | `storage_tiering.py` | Live |
| Store Reconciliation | `store_reconciliation.py` | Live — now checks Postgres and Qdrant only; the Neo4j leg is gone |
| Support Packet | `support_packet.py` | Live |
| Lineage Reporter | `lineage_reporter.py` | Live |
| Model Cost Summary | `model_cost_summary.py` | Live |
| Model Upgrade Watch | `model_upgrade_watch.py` | Live |
| Tenant Isolation Auditor | `tenant_isolation_auditor.py` | Live (Postgres RLS half) |
| Graph Tenant Auditor | `graph_tenant_auditor.py` | **Vestigial** — audits a graph store that no longer exists; still on a nightly cron ([Ch 07 §2.2](07-orchestration.md)) |
| LLM Incident Diagnosis | `llm_incident_diagnosis.py` | Experimental |
| Cost Burn Watcher | `cost_burn_watcher.py` (workflow, not `phase0/`) | Live |

Pydantic AI itself is vestigial: the guards live in
`orchestrator_validators.py`, not in an agent framework ([Ch 06](06-retrieval-and-agents.md)).

## Feature flags currently in play

| Flag | Default | Effect |
|---|---|---|
| `AGENTIC_RETRIEVAL_V2_ENABLED` | false (dev: true) | Use §04j LangGraph instead of legacy linear RAG |
| `GEO_ANSWER_OIUR_ENABLED` | false (dev: true) | Wrap answers in OIUR envelope |
| `OCR_ENGINE` | tesseract (compose: cohere_parse) | Selects Cohere Parse as primary scanned-page OCR (ADR-0019; on Bedrock since ADR-0022). Retired values fail loudly rather than downgrading silently |
| `BEDROCK_PARSE_MODEL_ID` | unset | The Bedrock Marketplace endpoint serving Parse. **Unset means every page runs Tesseract** after one CRITICAL log line — no table structure, no error. Replaces `AZURE_FOUNDRY_PARSE_DEPLOYMENT` |
| `PDF_PARSER_TESSERACT_FALLBACK_ENABLED` | true | Fall back to Tesseract when Parse is unavailable or empty |
| `OCR_ROUTING_THRESHOLDS_JSON` | unset | Calibrated multi-signal routing bands; unset fails closed to review |
| `P04P_DUAL_WRITE_ENABLED` | false | Run legacy parser in parallel for A/B |
| `CITATION_SPAN_RESOLVER_ENABLED` | false | Enable inline citation span resolver |
| `LLM_BACKEND` | `bedrock` | `bedrock` (Cohere Command A+ on a Bedrock Marketplace endpoint) / `vllm` (operator's own OpenAI-compatible endpoint) / `anthropic` (Claude, optional fallback). `azure` is REJECTED at startup, not ignored (ADR-0022) |
| `EMBEDDING_BACKEND` / `RERANKER_BACKEND` | `bedrock` | Code and compose both default to `bedrock` since 2026-09-08 — an unset value on a production task therefore selects Bedrock, not a model host that is not there. `.env.example` sets `local` / `cross_encoder` to use the dev sidecars. Set both explicitly in production, identically on the query and ingest paths |
| `LLM_BACKEND_FALLBACK` | `downshift` | Cross-backend failover policy |
| `LLM_FALLBACK_ENABLED` | false | Enable cross-backend failover |

## Known security items (tracked, not closed)

See appendix C. Summary:

1. **`georag` Postgres role is SUPERUSER + BYPASSRLS** — operationally
   mitigated, structural fix tracked (Ch 02 §1.1).
2. **Martin uses `georag_app` in dev** — `martin_readonly` exists, is documented as the role to use, and is still not the role used (Ch 02 §1.3).
3. **Qdrant auth off in dev** — deliberate (an empty `QDRANT__SERVICE__API_KEY` enables auth and breaks every client); must be set in prod (Ch 02 §2).
4. **External-LLM data egress** — the default backend is a managed model
   service (Bedrock since ADR-0022, Foundry before it), so every query
   leaves the container either way; the
   `georag_external_llm_egress_blocked_total` counter is the only signal
   and nothing scrapes it (Ch 12 §2.1).
5. **`persist_node` is best-effort** — answers can complete without an
   audit row (Ch 06 §2.1).
6. **`init-roles.sql` is outside the auto-init dir** — fresh clusters
   miss the read/write/audit roles (Ch 02).
7. **`docker commit` CMD trap** — explicit `command:` on every container
   protects against it, but a regression would silently swap entrypoints.
