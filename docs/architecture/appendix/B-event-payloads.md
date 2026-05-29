# Appendix B — Event Payloads

Exact JSON shapes for every event that crosses a process boundary in
GeoRAG. Each entry includes producer, consumer, channel/topic, payload,
example, and the schema test.

> **Status legend.** **L** = live shape on main; **P** = planned shape
> (events that exist but whose schema is still being formalised); **D** =
> draft — proposed, not yet emitted.

## 1. Reverb broadcast channels (Laravel)

All channels are private (Sanctum-authed). Echo client at
[resources/js/Lib/echo.ts](../../../resources/js/Lib/echo.ts).

### 1.1 `query.streaming.{run_id}` — chat streaming
Producer: FastAPI → Laravel `BroadcastQueryToken` event. Consumer: `Chat.tsx`.

**QueryToken** [L]
```json
{
  "event": "QueryToken",
  "data": {
    "run_id": "8d3e…",
    "delta": "The PLS-22-08 hole intersects ",
    "sequence": 42
  }
}
```

**QueryCitation** [L]
```json
{
  "event": "QueryCitation",
  "data": {
    "run_id": "8d3e…",
    "marker": "[ev:9a4f2d1c]",
    "evidence_id": "9a4f2d1c-…",
    "source_chunk_id": "passage_…",
    "source_kind": "document_passage",
    "source_table": "silver.document_passages",
    "source_pk": {"passage_id":"…"},
    "page_first": 14,
    "page_last": 14,
    "char_span": [102, 187],
    "confidence": 0.82
  }
}
```

**QueryComplete** [L]
```json
{
  "event": "QueryComplete",
  "data": {
    "run_id": "8d3e…",
    "answer_run_id": "5b1c…",
    "status": "completed",
    "intent": "factual_lookup",
    "tokens": {"prompt": 4120, "completion": 312, "total": 4432},
    "latency_ms": 5840,
    "backend_used": "vllm",
    "model": "Qwen/Qwen3-14B-AWQ",
    "confidence": 0.74,
    "citation_lifecycle_state": "resolved"
  }
}
```

**QueryRefusal** [L]
```json
{
  "event": "QueryRefusal",
  "data": {
    "run_id": "8d3e…",
    "refusal_reason": "retrieval_quality_below_threshold",
    "retrieval_quality": 0.41,
    "partial_evidence": [{"evidence_id":"…","confidence":0.41}]
  }
}
```

**QueryPersistFailure** [D] — proposed by [Ch 06 §2.1](../manual/06-retrieval-and-agents.md#21-persistence-is-currently-best-effort--fix-required)
```json
{
  "event": "QueryPersistFailure",
  "data": {
    "run_id": "8d3e…",
    "recoverable": false,
    "failure_class": "rls_misconfiguration"
  }
}
```

### 1.2 `ingestion-progress.{workspace_id}` — file ingestion progress
Producer: Hatchet workers + Dagster `commit_ingestion_run` → Laravel. Consumer: `IngestionRuns.tsx`, `DrillReview.tsx`.

**IngestProgress** [L]
```json
{
  "event": "IngestProgress",
  "data": {
    "ingest_run_id": "7f0e…",
    "project_id": "…",
    "file_id": "…",
    "sha256": "…",
    "stage": "parse",
    "step": "extract_tables",
    "status": "running",
    "pct_complete": 0.42,
    "page_first": 1,
    "page_last": 27,
    "page_current": 12,
    "started_at": "2026-05-25T22:13:01Z",
    "duration_s": 6.4,
    "error": null
  }
}
```

Terminal events of the same channel use `status` ∈ `{running, completed,
failed, dead_lettered}`.

### 1.3 `workspace-data-updated.{workspace_id}` — invalidation broadcast
Producer: Dagster `commit_ingestion_run`, Hatchet `score_targets`, Laravel mutation listeners. Consumer: every page that re-fetches on data change.

**WorkspaceDataUpdated** [L]
```json
{
  "event": "WorkspaceDataUpdated",
  "data": {
    "workspace_id": "a0000000-…",
    "data_version": 12347,
    "scopes": ["silver.collars", "silver.assays_v2", "gold.drillhole_intervals_visual"],
    "trigger": "dagster.commit_ingestion_run",
    "trigger_id": "run_…"
  }
}
```

Consumers diff their cached `data_version` and re-fetch only on bump.

### 1.4 `audit-ledger.{workspace_id}` — AuditLog real-time tail
Producer: trigger-driven via Laravel listener. Consumer: `AuditLog.tsx`.

**AuditEvent** [L]
```json
{
  "event": "AuditEvent",
  "data": {
    "id": 7129,
    "created_at": "2026-05-25T22:13:01.041Z",
    "workspace_id": "a0000000-…",
    "actor_id": 42,
    "actor_kind": "user",
    "action_type": "answer_runs.create",
    "target_schema": "silver",
    "target_table": "answer_runs",
    "target_id": "5b1c…",
    "trace_id": "01HF…"
  }
}
```

`hash`/`previous_hash` are intentionally omitted from the broadcast —
they live in the row and are exposed via the chain-verifier UI.

### 1.5 `notifications.{user_id}` — in-app notifications
Producer: Horizon `notifications` queue. Consumer: `Inbox.tsx`.

**Notification** [L]
```json
{
  "event": "Notification",
  "data": {
    "id": "01HF…",
    "kind": "ingestion_complete",
    "title": "PLS_2022_drill_data.zip processed",
    "body": "Imported 142 collars + 18,420 assay rows.",
    "url": "/projects/abc/drill-review?run=…",
    "severity": "info",
    "created_at": "2026-05-25T22:13:01Z"
  }
}
```

### 1.6 `support-replay.{run_id}` — Support Cockpit
Producer: Hatchet `support_replay`. Consumer: `SupportCockpit.tsx`.

**ReplayProgress** [L]
```json
{
  "event": "ReplayProgress",
  "data": {
    "replay_run_id": "rr_…",
    "original_run_id": "5b1c…",
    "stage": "retrieve",
    "pct_complete": 0.66,
    "diffs": [{"field":"retrieval.top_score","old":0.72,"new":0.68}]
  }
}
```

## 2. Hatchet workflow inputs/outputs

Hatchet input/output shapes are Pydantic models in
[src/fastapi/app/hatchet_workflows/](../../../src/fastapi/app/hatchet_workflows/).
The two load-bearing ones:

### 2.1 `ingest_pdf` (Hatchet `WORKER_POOL=ingestion`)

**Input** `IngestPdfInput`
([ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py))
```json
{
  "workspace_id": "a0000000-…",
  "project_id": "…",
  "minio_key": "bronze/a000…/proj/ab12cd…/file.pdf",
  "sha256": "ab12cd…",
  "uploaded_by_user_id": 42,
  "ingest_run_id": "7f0e…",
  "trace_id": "01HF…",
  "dual_write": false
}
```

**Step outputs:** `PreflightOut`, `ParseOut`, `IngestPdfFinalOut`. All
emit a Reverb `ingestion-progress.{workspace_id}` event before returning.

### 2.2 `outbox_dispatcher`

No input — cron-driven. Polls `outbox.pending_propagations`. Output per
row: insert into `outbox.propagation_attempts` with `status ∈
{succeeded, transient_failure, dead_lettered}`.

## 3. Dagster asset inputs/outputs

Dagster's IO is type-annotated Python — see each asset's docstring under
[src/dagster/georag_dagster/assets/](../../../src/dagster/georag_dagster/assets/).
The wire-format event that Dagster emits to Laravel is via
`commit_ingestion_run`:

**`commit_ingestion_run` → Laravel `/api/internal/v1/ingest-progress/broadcast`**
```json
{
  "ingest_run_id": "7f0e…",
  "workspace_id": "a0000000-…",
  "project_id": "…",
  "status": "completed",
  "asset_keys": ["silver_collars", "silver_lithology", "silver_assays_v2"],
  "row_counts": {"silver.collars": 142, "silver.assays_v2": 18420},
  "data_version": 12347
}
```

## 4. Kestra webhook + JWT payloads

### 4.1 Webhook envelope (canonical JSON the sender signs)
```json
{
  "notification_id": "uuid",
  "source": "string",
  "kind": "string",
  "payload": {...},
  "received_at": "ISO-8601"
}
```
- HMAC-SHA256 hex of the canonical JSON form (sorted keys, no whitespace,
  UTF-8) → `signature` field appended in POST body.
- Verified by the Hatchet `external_notification` workflow against
  `EXTERNAL_NOTIFICATION_HMAC_SECRET`.

### 4.2 Kestra → FastAPI JWT
- Algorithm `HS256` with per-flow private key (looked up from
  `workflow.flow_registry` and decrypted with `AUDIT_ENCRYPTION_KEY`).
- Header `kid: <flow_name>:<key_id>`.
- Claims:
```json
{
  "iss": "kestra",
  "sub": "<flow_name>",
  "aud": "fastapi",
  "iat": 1748209980,
  "exp": 1748210580,
  "jti": "uuid",
  "workspace_id": "uuid|null"
}
```
- 10-minute TTL; replay protected by `jti` cache (Redis db 0) until exp.

## 5. Outbox payload

`outbox.pending_propagations.payload JSONB` shape:
```json
{
  "target_store": "qdrant|neo4j|seaweedfs",
  "operation": "upsert|delete",
  "source_table": "silver.document_passages",
  "source_pk": {"passage_id": "…"},
  "workspace_id": "uuid",
  "snapshot": {...row body, captured at txn time...},
  "scheduled_at": "ISO-8601"
}
```

`propagation_attempts.status ∈ {succeeded, transient_failure,
dead_lettered}`; `last_error JSONB`.

## 6. SSE streaming (FastAPI → Laravel → Reverb)

FastAPI emits SSE events at `/v1/query/stream`. Laravel pipes them
straight onto the Reverb `query.streaming.{run_id}` channel. The SSE
event names map 1:1 to the Reverb event names in §1.1.

## 7. Internal Service-Key endpoint contracts

All `Authorization: X-Service-Key <FASTAPI_SERVICE_KEY>` endpoints
([src/fastapi/app/services/auth.py](../../../src/fastapi/app/services/auth.py)
on the FastAPI side):

| Endpoint | Direction | Purpose |
|---|---|---|
| `POST /internal/v1/shadow/ingest_pdf/trigger` | Laravel → FastAPI | Kicks off a Hatchet `ingest_pdf` workflow |
| `POST /internal/v1/integrations/external_notification/trigger` | Kestra → FastAPI | Inbound webhook entrypoint |
| `POST /api/internal/v1/ingest-progress/broadcast` | FastAPI/Hatchet/Dagster → Laravel | Reverb fan-out for ingestion progress |
| `POST /api/internal/v1/broadcast/workspace-data-updated` | FastAPI/Hatchet/Dagster → Laravel | Reverb fan-out for workspace_data_updated |
| `POST /api/internal/v1/re-ocr` | Laravel → FastAPI | Triggers `re_ocr_page` Hatchet workflow |

## 8. Authoritative test coverage

- Reverb event payload tests: `tests/Feature/Broadcast/*`.
- Hatchet step IO tests: `src/fastapi/tests/test_hatchet_*`.
- Dagster asset IO: `src/dagster/tests/test_assets_*`.
- Kestra webhook signature: `tests/Feature/Kestra/HmacSignatureTest.php`.

(These test paths are the convention; not every event currently has a
test — add when shapes change.)

## B.7 Workflow + asset I/O catalog

This catalog is generated by static AST analysis of every Hatchet workflow in [src/fastapi/app/hatchet_workflows/](../../../src/fastapi/app/hatchet_workflows/) and every Dagster asset in [src/dagster/georag_dagster/assets/](../../../src/dagster/georag_dagster/assets/). It is auditable — re-run the generator (Z.5 task) to refresh. Each row links the file plus line refs for the workflow declaration, the Pydantic input model, and the terminal-step output model. Untyped slots (`dict`, `Any`, missing annotation) are flagged with ⚠️ so coverage gaps are visible at a glance.

*Last refreshed: 2026-05-29.*

### B.7.1 Hatchet workflows

**Coverage:** 53 workflows discovered (53 fully typed in+out). Archived workflows under `_archived/` are listed for completeness. The 10 Phase 0 agent workflows in `phase0_agents.py` were retrofitted with typed Pydantic v2 output models on 2026-05-29 (closes §B.7.1 untyped gap; see `feat(phase0): type 10 Hatchet workflow outputs`).

| name | file | input schema | output schema | line refs |
|---|---|---|---|---|
| `audit_ledger_verify` | [src/fastapi/app/hatchet_workflows/audit_ledger_verify.py](../../../src/fastapi/app/hatchet_workflows/audit_ledger_verify.py) | `AuditVerifyInput` | `AuditVerifyOutput` | decl=L45, in=L26, out=L37 |
| `backup_neo4j` | [src/fastapi/app/hatchet_workflows/backup_neo4j.py](../../../src/fastapi/app/hatchet_workflows/backup_neo4j.py) | `BackupNeo4jInput` | `BackupNeo4jOutput` | decl=L94, in=L62, out=L83 |
| `backup_postgres` | [src/fastapi/app/hatchet_workflows/backup_postgres.py](../../../src/fastapi/app/hatchet_workflows/backup_postgres.py) | `BackupPostgresInput` | `BackupPostgresOutput` | decl=L82, in=L58, out=L71 |
| `backup_qdrant` | [src/fastapi/app/hatchet_workflows/backup_qdrant.py](../../../src/fastapi/app/hatchet_workflows/backup_qdrant.py) | `BackupQdrantInput` | `BackupQdrantOutput` | decl=L66, in=L50, out=L55 |
| `backup_redis` | [src/fastapi/app/hatchet_workflows/backup_redis.py](../../../src/fastapi/app/hatchet_workflows/backup_redis.py) | `BackupRedisInput` | `BackupRedisOutput` | decl=L66, in=L48, out=L55 |
| `backup_seaweedfs` | [src/fastapi/app/hatchet_workflows/backup_seaweedfs.py](../../../src/fastapi/app/hatchet_workflows/backup_seaweedfs.py) | `BackupSeaweedFsInput` | `BackupSeaweedFsOutput` | decl=L82, in=L53, out=L69 |
| `cold_tier_archive` | [src/fastapi/app/hatchet_workflows/cold_tier_archive.py](../../../src/fastapi/app/hatchet_workflows/cold_tier_archive.py) | `ColdTierArchiveInput` | `ColdTierArchiveOutput` | decl=L87, in=L56, out=L76 |
| `continuous_learning_loop` | [src/fastapi/app/hatchet_workflows/continuous_learning_loop.py](../../../src/fastapi/app/hatchet_workflows/continuous_learning_loop.py) | `ContinuousLearningLoopInput` | `ContinuousLearningLoopOutput` | decl=L82, in=L50, out=L62 |
| `cost_burn_watcher` | [src/fastapi/app/hatchet_workflows/cost_burn_watcher.py](../../../src/fastapi/app/hatchet_workflows/cost_burn_watcher.py) | `CostBurnWatcherInput` | `CostBurnWatcherOutput` | decl=L84, in=L63, out=L75 |
| `embed_pending_passages` | [src/fastapi/app/hatchet_workflows/embed_pending_passages.py](../../../src/fastapi/app/hatchet_workflows/embed_pending_passages.py) | `EmbedPendingPassagesInput` | `EmbedPendingPassagesOutput` | decl=L73, in=L35, out=L52 |
| `eval_real_rag_nightly` | [src/fastapi/app/hatchet_workflows/eval_real_rag_nightly.py](../../../src/fastapi/app/hatchet_workflows/eval_real_rag_nightly.py) | `EvalRealRagNightlyInput` | `EvalRealRagNightlyOutput` | decl=L157, in=L46, out=L67 |
| `evaluate_workspace` | [src/fastapi/app/hatchet_workflows/evaluate_workspace.py](../../../src/fastapi/app/hatchet_workflows/evaluate_workspace.py) | `EvaluateWorkspaceInput` | `EvaluateWorkspaceOutput` | decl=L86, in=L45, out=L72 |
| `external_notification` | [src/fastapi/app/hatchet_workflows/external_notification.py](../../../src/fastapi/app/hatchet_workflows/external_notification.py) | `ExternalNotificationInput` | `ExternalNotificationOut` | decl=L318, in=L58, out=L85 |
| `field_outcome_learning` | [src/fastapi/app/hatchet_workflows/field_outcome_learning.py](../../../src/fastapi/app/hatchet_workflows/field_outcome_learning.py) | `FieldOutcomeLearningInput` | `FieldOutcomeLearningOutput` | decl=L68, in=L39, out=L50 |
| `flow_jwt_key_reaper` | [src/fastapi/app/hatchet_workflows/flow_jwt_key_reaper.py](../../../src/fastapi/app/hatchet_workflows/flow_jwt_key_reaper.py) | `FlowJwtKeyReaperInput` | `FlowJwtKeyReaperOutput` | decl=L39, in=L26, out=L34 |
| `generate_report` | [src/fastapi/app/hatchet_workflows/generate_report.py](../../../src/fastapi/app/hatchet_workflows/generate_report.py) | `GenerateReportInput` | `GenerateReportOutput` | decl=L98, in=L44, out=L72 |
| `idempotency_keys_cleanup` | [src/fastapi/app/hatchet_workflows/idempotency_keys_cleanup.py](../../../src/fastapi/app/hatchet_workflows/idempotency_keys_cleanup.py) | `CleanupInput` | `CleanupOut` | decl=L64, in=L37, out=L49 |
| `index_health_check` | [src/fastapi/app/hatchet_workflows/phase0_agents.py](../../../src/fastapi/app/hatchet_workflows/phase0_agents.py) | `AgentRunInput` | `IndexHealthCheckOutput` | decl=L287, in=L112, out=L269 |
| `ingest_pdf` | [src/fastapi/app/hatchet_workflows/ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py) | `IngestPdfInput` | `IngestPdfFinalOut` | decl=L454, in=L305, out=L357 |
| `lineage_walk` | [src/fastapi/app/hatchet_workflows/phase0_agents.py](../../../src/fastapi/app/hatchet_workflows/phase0_agents.py) | `AgentRunInput` | `LineageWalkOutput` | decl=L215, in=L112, out=L199 |
| `llm_incident_diagnosis_run` | [src/fastapi/app/hatchet_workflows/phase0_agents.py](../../../src/fastapi/app/hatchet_workflows/phase0_agents.py) | `AgentRunInput` | `LlmIncidentDiagnosisRunOutput` | decl=L464, in=L112, out=L446 |
| `model_cost_summary_run` | [src/fastapi/app/hatchet_workflows/phase0_agents.py](../../../src/fastapi/app/hatchet_workflows/phase0_agents.py) | `AgentRunInput` | `ModelCostSummaryRunOutput` | decl=L427, in=L112, out=L411 |
| `model_upgrade_watch_run` | [src/fastapi/app/hatchet_workflows/phase0_agents.py](../../../src/fastapi/app/hatchet_workflows/phase0_agents.py) | `AgentRunInput` | `ModelUpgradeWatchRunOutput` | decl=L355, in=L112, out=L339 |
| `mv_refresh_silver` | [src/fastapi/app/hatchet_workflows/mv_refresh_silver.py](../../../src/fastapi/app/hatchet_workflows/mv_refresh_silver.py) | `MvRefreshSilverInput` | `MvRefreshSilverOutput` | decl=L44, in=L31, out=L40 |
| `nightly_ingestion_integrity` | [src/fastapi/app/hatchet_workflows/nightly_ingestion_integrity.py](../../../src/fastapi/app/hatchet_workflows/nightly_ingestion_integrity.py) | `NightlyIntegritySweepInput` | `NightlyIntegritySweepOutput` | decl=L138, in=L112, out=L130 |
| `ocr_quality_check` | [src/fastapi/app/hatchet_workflows/ocr_quality_check.py](../../../src/fastapi/app/hatchet_workflows/ocr_quality_check.py) | `OcrQualityCheckInput` | `OcrQualityCheckOutput` | decl=L73, in=L50, out=L62 |
| `outbox_dispatcher` | [src/fastapi/app/hatchet_workflows/outbox_dispatcher.py](../../../src/fastapi/app/hatchet_workflows/outbox_dispatcher.py) | `OutboxDispatcherInput` | `OutboxDispatcherOutput` | decl=L67, in=L52, out=L59 |
| `phase2_smoke` | [src/fastapi/app/hatchet_workflows/phase2_smoke.py](../../../src/fastapi/app/hatchet_workflows/phase2_smoke.py) | `Phase2SmokeInput` | `Phase2SmokeOut` | decl=L42, in=L29, out=L36 |
| `public_geoscience_pull` | [src/fastapi/app/hatchet_workflows/public_geoscience_pull.py](../../../src/fastapi/app/hatchet_workflows/public_geoscience_pull.py) | `PublicGeoSciencePullInput` | `PublicGeoSciencePullOut` | decl=L147, in=L47, out=L58 |
| `re_ocr_page` | [src/fastapi/app/hatchet_workflows/re_ocr_page.py](../../../src/fastapi/app/hatchet_workflows/re_ocr_page.py) | `ReOcrPageInput` | `ReOcrPageOutput` | decl=L80, in=L56, out=L72 |
| `reliability_metrics_publisher` | [src/fastapi/app/hatchet_workflows/reliability_metrics_publisher.py](../../../src/fastapi/app/hatchet_workflows/reliability_metrics_publisher.py) | `ReliabilityMetricsPublisherInput` | `ReliabilityMetricsPublisherOutput` | decl=L42, in=L33, out=L37 |
| `repair_shadow_aggregate` | [src/fastapi/app/hatchet_workflows/repair_shadow_aggregate.py](../../../src/fastapi/app/hatchet_workflows/repair_shadow_aggregate.py) | `RepairShadowAggregateInput` | `RepairShadowAggregateOutput` | decl=L90, in=L57, out=L78 |
| `restore_workspace` | [src/fastapi/app/hatchet_workflows/restore_workspace.py](../../../src/fastapi/app/hatchet_workflows/restore_workspace.py) | `RestoreWorkspaceInput` | `RestoreWorkspaceOutput` | decl=L98, in=L69, out=L88 |
| `score_targets` | [src/fastapi/app/hatchet_workflows/score_targets.py](../../../src/fastapi/app/hatchet_workflows/score_targets.py) | `ScoreTargetsInput` | `ScoreTargetsOutput` | decl=L106, in=L46, out=L85 |
| `shadow_diff` | [src/fastapi/app/hatchet_workflows/shadow_diff.py](../../../src/fastapi/app/hatchet_workflows/shadow_diff.py) | `ShadowDiffInput` | `ShadowDiffFinalOut` | decl=L106, in=L46, out=L50 |
| `shadow_diff_scan` | [src/fastapi/app/hatchet_workflows/shadow_diff.py](../../../src/fastapi/app/hatchet_workflows/shadow_diff.py) | `ShadowDiffScanInput` | `ShadowDiffScanOut` | decl=L268, in=L58, out=L64 |
| `stale_run_detector` | [src/fastapi/app/hatchet_workflows/stale_run_detector.py](../../../src/fastapi/app/hatchet_workflows/stale_run_detector.py) | `StaleRunDetectorInput` | `StaleRunDetectorOutput` | decl=L106, in=L89, out=L97 |
| `storage_tiering_run` | [src/fastapi/app/hatchet_workflows/phase0_agents.py](../../../src/fastapi/app/hatchet_workflows/phase0_agents.py) | `AgentRunInput` | `StorageTieringRunOutput` | decl=L250, in=L112, out=L233 |
| `store_reconciliation_run` | [src/fastapi/app/hatchet_workflows/phase0_agents.py](../../../src/fastapi/app/hatchet_workflows/phase0_agents.py) | `AgentRunInput` | `StoreReconciliationRunOutput` | decl=L320, in=L112, out=L306 |
| `support_packet_assemble` | [src/fastapi/app/hatchet_workflows/phase0_agents.py](../../../src/fastapi/app/hatchet_workflows/phase0_agents.py) | `AgentRunInput` | `SupportPacketAssembleOutput` | decl=L501, in=L112, out=L482 |
| `support_replay` | [src/fastapi/app/hatchet_workflows/support_replay.py](../../../src/fastapi/app/hatchet_workflows/support_replay.py) | `SupportReplayInput` | `SupportReplayOutput` | decl=L90, in=L58, out=L75 |
| `sync_silver_to_kg` | [src/fastapi/app/hatchet_workflows/sync_silver_to_kg.py](../../../src/fastapi/app/hatchet_workflows/sync_silver_to_kg.py) | `SyncSilverToKGInput` | `SyncSilverToKGOutput` | decl=L92, in=L38, out=L52 |
| `tenant_isolation_audit` | [src/fastapi/app/hatchet_workflows/phase0_agents.py](../../../src/fastapi/app/hatchet_workflows/phase0_agents.py) | `AgentRunInput` | `TenantIsolationAuditOutput` | decl=L157, in=L112, out=L137 |
| `tiff_normalize` | [src/fastapi/app/hatchet_workflows/tiff_normalize.py](../../../src/fastapi/app/hatchet_workflows/tiff_normalize.py) | `TiffNormalizeInput` | `TiffNormalizeOutput` | decl=L137, in=L47, out=L66 |
| `tiff_ocr_cluster` | [src/fastapi/app/hatchet_workflows/tiff_ocr_cluster.py](../../../src/fastapi/app/hatchet_workflows/tiff_ocr_cluster.py) | `TiffOcrClusterInput` | `TiffOcrClusterOutput` | decl=L76, in=L37, out=L66 |
| `train_source_trust` | [src/fastapi/app/hatchet_workflows/train_source_trust.py](../../../src/fastapi/app/hatchet_workflows/train_source_trust.py) | `TrainSourceTrustInput` | `TrainSourceTrustOutput` | decl=L89, in=L52, out=L70 |
| `train_target_model` | [src/fastapi/app/hatchet_workflows/train_target_model.py](../../../src/fastapi/app/hatchet_workflows/train_target_model.py) | `TrainTargetModelInput` | `TrainTargetModelOutput` | decl=L86, in=L42, out=L67 |
| `vllm_security_check_run` | [src/fastapi/app/hatchet_workflows/phase0_agents.py](../../../src/fastapi/app/hatchet_workflows/phase0_agents.py) | `AgentRunInput` | `VllmSecurityCheckRunOutput` | decl=L392, in=L112, out=L374 |
| `what_changed_detector` | [src/fastapi/app/hatchet_workflows/what_changed_detector.py](../../../src/fastapi/app/hatchet_workflows/what_changed_detector.py) | `WhatChangedInput` | `WhatChangedOutput` | decl=L72, in=L45, out=L53 |
| `what_changed_weekly` | [src/fastapi/app/hatchet_workflows/what_changed_weekly.py](../../../src/fastapi/app/hatchet_workflows/what_changed_weekly.py) | `WeeklyDigestInput` | `WeeklyDigestOutput` | decl=L79, in=L41, out=L69 |
| `workspace_export` | [src/fastapi/app/hatchet_workflows/workspace_export.py](../../../src/fastapi/app/hatchet_workflows/workspace_export.py) | `WorkspaceExportInput` | `WorkspaceExportOutput` | decl=L122, in=L84, out=L104 |
| `shadow_diff (archived)` | [src/fastapi/app/hatchet_workflows/_archived/shadow_diff.py](../../../src/fastapi/app/hatchet_workflows/_archived/shadow_diff.py) | `ShadowDiffInput` | `ShadowDiffFinalOut` | decl=L106, in=L46, out=L50 |
| `shadow_diff_scan (archived)` | [src/fastapi/app/hatchet_workflows/_archived/shadow_diff.py](../../../src/fastapi/app/hatchet_workflows/_archived/shadow_diff.py) | `ShadowDiffScanInput` | `ShadowDiffScanOut` | decl=L268, in=L58, out=L64 |

### B.7.2 Dagster assets

**Coverage:** 94 assets discovered across 11 group_names (94 fully typed return; no untyped slots remaining). `MaterializeResult` is Dagster's native typed result envelope — metadata + asset key — so it counts as typed; assets that return `Output[T]` further pin the materialized payload's type.

| name | group / compute_kind | file | inputs | output | line refs |
|---|---|---|---|---|---|
| `bronze_collars` | bronze / — | [src/dagster/georag_dagster/assets/bronze.py](../../../src/dagster/georag_dagster/assets/bronze.py) | `config: BronzeCollarsConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L63, fn=L70 |
| `bronze_geophysics` | bronze / — | [src/dagster/georag_dagster/assets/bronze_geophysics.py](../../../src/dagster/georag_dagster/assets/bronze_geophysics.py) | `config: BronzeGeophysicsConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L74, fn=L82 |
| `bronze_lithology` | bronze / — | [src/dagster/georag_dagster/assets/bronze_lithology.py](../../../src/dagster/georag_dagster/assets/bronze_lithology.py) | `config: BronzeLithologyConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L47, fn=L55 |
| `bronze_pg_ca_bc_minfile` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L609, fn=L619 |
| `bronze_pg_ca_sk_assessment_airborne` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L587, fn=L594 |
| `bronze_pg_ca_sk_assessment_ground` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L565, fn=L572 |
| `bronze_pg_ca_sk_assessment_underground` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L543, fn=L550 |
| `bronze_pg_ca_sk_bedrock_geology` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L1057, fn=L1066 |
| `bronze_pg_ca_sk_drillhole` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L498, fn=L506 |
| `bronze_pg_ca_sk_mine_loc` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L451, fn=L460 |
| `bronze_pg_ca_sk_mineral_disposition` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L922, fn=L933 |
| `bronze_pg_ca_sk_resource_potential` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L634, fn=L644 |
| `bronze_pg_ca_sk_rock_samples` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L521, fn=L528 |
| `bronze_pg_ca_sk_smdi` | bronze / — | [src/dagster/georag_dagster/assets/bronze_public_geoscience.py](../../../src/dagster/georag_dagster/assets/bronze_public_geoscience.py) | `config: BronzePublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L475, fn=L483 |
| `bronze_raster_uploads` | bronze / — | [src/dagster/georag_dagster/assets/silver_cog_rasters.py](../../../src/dagster/georag_dagster/assets/silver_cog_rasters.py) | `minio: S3Resource` | `MaterializeResult` | deco=L131, fn=L140 |
| `bronze_reports` | bronze / — | [src/dagster/georag_dagster/assets/bronze_reports.py](../../../src/dagster/georag_dagster/assets/bronze_reports.py) | `config: BronzeReportsConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L25, fn=L32 |
| `bronze_samples` | bronze / — | [src/dagster/georag_dagster/assets/bronze_samples.py](../../../src/dagster/georag_dagster/assets/bronze_samples.py) | `config: BronzeSamplesConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L35, fn=L42 |
| `bronze_seismic` | bronze / — | [src/dagster/georag_dagster/assets/bronze_seismic.py](../../../src/dagster/georag_dagster/assets/bronze_seismic.py) | `config: BronzeSeismicConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L27, fn=L34 |
| `bronze_spatial` | bronze / — | [src/dagster/georag_dagster/assets/bronze_spatial.py](../../../src/dagster/georag_dagster/assets/bronze_spatial.py) | `config: BronzeSpatialConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L39, fn=L46 |
| `bronze_surveys` | bronze / — | [src/dagster/georag_dagster/assets/bronze_surveys.py](../../../src/dagster/georag_dagster/assets/bronze_surveys.py) | `config: BronzeSurveysConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L34, fn=L41 |
| `bronze_well_logs` | bronze / — | [src/dagster/georag_dagster/assets/bronze_well_logs.py](../../../src/dagster/georag_dagster/assets/bronze_well_logs.py) | `config: BronzeWellLogsConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L28, fn=L35 |
| `bronze_xlsx` | bronze / — | [src/dagster/georag_dagster/assets/bronze_xlsx.py](../../../src/dagster/georag_dagster/assets/bronze_xlsx.py) | `config: BronzeXlsxConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L27, fn=L34 |
| `bronze_xyz` | bronze / — | [src/dagster/georag_dagster/assets/bronze_xyz.py](../../../src/dagster/georag_dagster/assets/bronze_xyz.py) | `config: BronzeXyzConfig`<br>`minio: S3Resource` | `MaterializeResult` | deco=L46, fn=L53 |
| `commit_ingestion_run` | commit / — | [src/dagster/georag_dagster/assets/commit_ingestion_run.py](../../../src/dagster/georag_dagster/assets/commit_ingestion_run.py) | `config: CommitIngestionRunConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L470, fn=L494 |
| `silver_assay_dq` | data_quality / postgres | [src/dagster/georag_dagster/assets/silver_assay_dq.py](../../../src/dagster/georag_dagster/assets/silver_assay_dq.py) | `config: AssayDQConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L422, fn=L434 |
| `silver_collar_dq` | data_quality / postgres | [src/dagster/georag_dagster/assets/silver_collar_dq.py](../../../src/dagster/georag_dagster/assets/silver_collar_dq.py) | `config: CollarDQConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L211, fn=L221 |
| `silver_crs_dq` | data_quality / postgres | [src/dagster/georag_dagster/assets/silver_crs_dq.py](../../../src/dagster/georag_dagster/assets/silver_crs_dq.py) | `config: CrsDQConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L240, fn=L251 |
| `silver_unit_consistency_dq` | data_quality / postgres | [src/dagster/georag_dagster/assets/silver_unit_consistency_dq.py](../../../src/dagster/georag_dagster/assets/silver_unit_consistency_dq.py) | `config: UnitConsistencyDQConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L251, fn=L262 |
| `gold_assay_composites` | drillhole_gold / — | [src/dagster/georag_dagster/assets/silver_to_gold/assay_composites.py](../../../src/dagster/georag_dagster/assets/silver_to_gold/assay_composites.py) | `config: AssayCompositesConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L81, fn=L89 |
| `gold_campaign_summaries` | drillhole_gold / — | [src/dagster/georag_dagster/assets/silver_to_gold/campaign_summaries.py](../../../src/dagster/georag_dagster/assets/silver_to_gold/campaign_summaries.py) | `config: CampaignSummariesConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L88, fn=L92 |
| `gold_drill_summaries` | drillhole_gold / — | [src/dagster/georag_dagster/assets/silver_to_gold/drill_summaries.py](../../../src/dagster/georag_dagster/assets/silver_to_gold/drill_summaries.py) | `config: DrillSummariesConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L107, fn=L115 |
| `gold_element_correlations` | drillhole_gold / — | [src/dagster/georag_dagster/assets/silver_to_gold/element_correlations.py](../../../src/dagster/georag_dagster/assets/silver_to_gold/element_correlations.py) | `config: ElementCorrelationsConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L75, fn=L83 |
| `gold_qaqc_statistics` | drillhole_gold / — | [src/dagster/georag_dagster/assets/silver_to_gold/qaqc_statistics.py](../../../src/dagster/georag_dagster/assets/silver_to_gold/qaqc_statistics.py) | `config: QaqcStatisticsConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L69, fn=L78 |
| `gold_significant_intersections` | drillhole_gold / — | [src/dagster/georag_dagster/assets/silver_to_gold/significant_intersections.py](../../../src/dagster/georag_dagster/assets/silver_to_gold/significant_intersections.py) | `config: SignificantIntersectionsConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L112, fn=L121 |
| `gold_zone_statistics` | drillhole_gold / — | [src/dagster/georag_dagster/assets/silver_to_gold/zone_statistics.py](../../../src/dagster/georag_dagster/assets/silver_to_gold/zone_statistics.py) | `config: ZoneStatisticsConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L61, fn=L69 |
| `silver_alteration` | drillhole_silver / — | [src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py](../../../src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py) | `config: _DrillholeStubConfig` | `MaterializeResult` | deco=L68, fn=L72 |
| `silver_assays_v2` | drillhole_silver / — | [src/dagster/georag_dagster/assets/bronze_to_silver/assays.py](../../../src/dagster/georag_dagster/assets/bronze_to_silver/assays.py) | `config: SilverAssaysV2Config`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L73, fn=L81 |
| `silver_geotechnical` | drillhole_silver / — | [src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py](../../../src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py) | `config: _DrillholeStubConfig` | `MaterializeResult` | deco=L94, fn=L98 |
| `silver_lithology_v2` | drillhole_silver / — | [src/dagster/georag_dagster/assets/bronze_to_silver/lithology.py](../../../src/dagster/georag_dagster/assets/bronze_to_silver/lithology.py) | `config: SilverLithologyConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L120, fn=L131 |
| `silver_mineralization` | drillhole_silver / — | [src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py](../../../src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py) | `config: _DrillholeStubConfig` | `MaterializeResult` | deco=L81, fn=L85 |
| `silver_qaqc_results` | drillhole_silver / — | [src/dagster/georag_dagster/assets/bronze_to_silver/qaqc.py](../../../src/dagster/georag_dagster/assets/bronze_to_silver/qaqc.py) | `config: SilverQaqcConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L35, fn=L43 |
| `silver_recovery` | drillhole_silver / — | [src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py](../../../src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py) | `config: _DrillholeStubConfig` | `MaterializeResult` | deco=L29, fn=L33 |
| `silver_specific_gravity` | drillhole_silver / — | [src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py](../../../src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py) | `config: _DrillholeStubConfig` | `MaterializeResult` | deco=L42, fn=L46 |
| `silver_structure` | drillhole_silver / — | [src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py](../../../src/dagster/georag_dagster/assets/bronze_to_silver/_stubs.py) | `config: _DrillholeStubConfig` | `MaterializeResult` | deco=L55, fn=L59 |
| `gold_cross_corpus_linker` | gold / — | [src/dagster/georag_dagster/assets/gold_cross_corpus_linker.py](../../../src/dagster/georag_dagster/assets/gold_cross_corpus_linker.py) | `config: CrossCorpusLinkerConfig`<br>`postgres: PostgresResource`<br>`neo4j: Neo4jResource` | `MaterializeResult` | deco=L793, fn=L811 |
| `gold_cross_section_panels` | gold / — | [src/dagster/georag_dagster/assets/gold_cross_section_panels.py](../../../src/dagster/georag_dagster/assets/gold_cross_section_panels.py) | `config: GoldCrossSectionConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L173, fn=L181 |
| `gold_drillhole_intervals_visual` | gold / — | [src/dagster/georag_dagster/assets/gold_drillhole_intervals_visual.py](../../../src/dagster/georag_dagster/assets/gold_drillhole_intervals_visual.py) | `postgres: PostgresResource` | `MaterializeResult` | deco=L122, fn=L133 |
| `gold_h3_density_choropleth` | gold / — | [src/dagster/georag_dagster/assets/gold_h3_density.py](../../../src/dagster/georag_dagster/assets/gold_h3_density.py) | `postgres: PostgresResource` | `MaterializeResult` | deco=L151, fn=L162 |
| `gold_public_geoscience_neo4j` | gold / — | [src/dagster/georag_dagster/assets/gold_public_geoscience.py](../../../src/dagster/georag_dagster/assets/gold_public_geoscience.py) | `config: GoldPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`neo4j: Neo4jResource` | `MaterializeResult` | deco=L685, fn=L708 |
| `gold_structure_measurements_visual` | gold / — | [src/dagster/georag_dagster/assets/gold_structure_measurements_visual.py](../../../src/dagster/georag_dagster/assets/gold_structure_measurements_visual.py) | `config: GoldStereonetConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L129, fn=L137 |
| `index_document_passages` | index / qdrant | [src/dagster/georag_dagster/assets/index_document_passages.py](../../../src/dagster/georag_dagster/assets/index_document_passages.py) | `config: IndexDocumentPassagesConfig`<br>`postgres: PostgresResource`<br>`qdrant: QdrantResource` | `MaterializeResult` | deco=L451, fn=L461 |
| `index_neo4j` | index / — | [src/dagster/georag_dagster/assets/index_neo4j.py](../../../src/dagster/georag_dagster/assets/index_neo4j.py) | `config: IndexNeo4jConfig`<br>`postgres: PostgresResource`<br>`neo4j: Neo4jResource` | `MaterializeResult` | deco=L244, fn=L254 |
| `index_public_geoscience_qdrant` | index / — | [src/dagster/georag_dagster/assets/index_public_geoscience.py](../../../src/dagster/georag_dagster/assets/index_public_geoscience.py) | `config: IndexPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`qdrant: QdrantResource` | `MaterializeResult` | deco=L848, fn=L868 |
| `index_reports` | index / — | [src/dagster/georag_dagster/assets/index_reports.py](../../../src/dagster/georag_dagster/assets/index_reports.py) | `config: IndexReportsConfig`<br>`postgres: PostgresResource`<br>`qdrant: QdrantResource`<br>`minio: 'S3Resource'` | `MaterializeResult` | deco=L370, fn=L379 |
| `silver_samples_nl_summary` | nl_summaries / postgres | [src/dagster/georag_dagster/assets/silver_samples_nl_summary.py](../../../src/dagster/georag_dagster/assets/silver_samples_nl_summary.py) | `postgres: PostgresResource` | `MaterializeResult` | deco=L153, fn=L164 |
| `silver_cog_rasters` | silver / — | [src/dagster/georag_dagster/assets/silver_cog_rasters.py](../../../src/dagster/georag_dagster/assets/silver_cog_rasters.py) | `minio: S3Resource` | `MaterializeResult` | deco=L280, fn=L289 |
| `silver_collars` | silver / — | [src/dagster/georag_dagster/assets/silver.py](../../../src/dagster/georag_dagster/assets/silver.py) | `config: SilverCollarsConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L187, fn=L204 |
| `silver_collars_canonicalize_backfill` | silver / — | [src/dagster/georag_dagster/assets/silver_collars_canonicalize_backfill.py](../../../src/dagster/georag_dagster/assets/silver_collars_canonicalize_backfill.py) | `config: SilverCollarsCanonicalizeBackfillConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L80, fn=L91 |
| `silver_drill_traces` | silver / — | [src/dagster/georag_dagster/assets/silver_drill_traces.py](../../../src/dagster/georag_dagster/assets/silver_drill_traces.py) | `postgres: PostgresResource` | `MaterializeResult` | deco=L368, fn=L378 |
| `silver_entity_ner_backfill` | silver / — | [src/dagster/georag_dagster/assets/silver_entity_ner_backfill.py](../../../src/dagster/georag_dagster/assets/silver_entity_ner_backfill.py) | `config: SilverEntityNerBackfillConfig`<br>`postgres: PostgresResource`<br>`neo4j: Neo4jResource` | `MaterializeResult` | deco=L659, fn=L676 |
| `silver_geochronology_samples` | silver / — | [src/dagster/georag_dagster/assets/silver_geochronology.py](../../../src/dagster/georag_dagster/assets/silver_geochronology.py) | `config: SilverGeochronologyConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L121, fn=L130 |
| `silver_geophysics` | silver / — | [src/dagster/georag_dagster/assets/silver_geophysics.py](../../../src/dagster/georag_dagster/assets/silver_geophysics.py) | `config: SilverGeophysicsConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L126, fn=L134 |
| `silver_lithology` | silver / — | [src/dagster/georag_dagster/assets/silver_lithology.py](../../../src/dagster/georag_dagster/assets/silver_lithology.py) | `config: SilverLithologyConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L130, fn=L140 |
| `silver_pg_ca_bc_minfile` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L2163, fn=L2174 |
| `silver_pg_ca_sk_assessment_airborne` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L2142, fn=L2147 |
| `silver_pg_ca_sk_assessment_ground` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L2121, fn=L2126 |
| `silver_pg_ca_sk_assessment_underground` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L2100, fn=L2105 |
| `silver_pg_ca_sk_bedrock_geology` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L2422, fn=L2431 |
| `silver_pg_ca_sk_drillhole` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L2046, fn=L2056 |
| `silver_pg_ca_sk_mine_loc` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L1984, fn=L1994 |
| `silver_pg_ca_sk_mineral_disposition` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L2329, fn=L2342 |
| `silver_pg_ca_sk_resource_potential` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L2197, fn=L2208 |
| `silver_pg_ca_sk_rock_samples` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L2079, fn=L2084 |
| `silver_pg_ca_sk_smdi` | silver / — | [src/dagster/georag_dagster/assets/silver_public_geoscience.py](../../../src/dagster/georag_dagster/assets/silver_public_geoscience.py) | `config: SilverPublicGeoscienceConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L2015, fn=L2023 |
| `silver_raster` | silver / — | [src/dagster/georag_dagster/assets/silver_raster.py](../../../src/dagster/georag_dagster/assets/silver_raster.py) | `config: SilverRasterConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L207, fn=L215 |
| `silver_reports` | silver / — | [src/dagster/georag_dagster/assets/silver_reports.py](../../../src/dagster/georag_dagster/assets/silver_reports.py) | `config: SilverReportsConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L144, fn=L152 |
| `silver_samples` | silver / — | [src/dagster/georag_dagster/assets/silver_samples.py](../../../src/dagster/georag_dagster/assets/silver_samples.py) | `config: SilverSamplesConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L137, fn=L147 |
| `silver_seismic` | silver / — | [src/dagster/georag_dagster/assets/silver_seismic.py](../../../src/dagster/georag_dagster/assets/silver_seismic.py) | `config: SilverSeismicConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L148, fn=L157 |
| `silver_spatial` | silver / — | [src/dagster/georag_dagster/assets/silver_spatial.py](../../../src/dagster/georag_dagster/assets/silver_spatial.py) | `config: SilverSpatialConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L243, fn=L251 |
| `silver_structure_derive` | silver / — | [src/dagster/georag_dagster/assets/silver_structure_derive.py](../../../src/dagster/georag_dagster/assets/silver_structure_derive.py) | `config: SilverStructureDeriveConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L151, fn=L161 |
| `silver_structure_populate` | silver / — | [src/dagster/georag_dagster/assets/silver_structure_populate.py](../../../src/dagster/georag_dagster/assets/silver_structure_populate.py) | `config: SilverStructurePopulateConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L513, fn=L526 |
| `silver_surveys` | silver / — | [src/dagster/georag_dagster/assets/silver_surveys.py](../../../src/dagster/georag_dagster/assets/silver_surveys.py) | `config: SilverSurveysConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L112, fn=L122 |
| `silver_well_logs` | silver / — | [src/dagster/georag_dagster/assets/silver_well_logs.py](../../../src/dagster/georag_dagster/assets/silver_well_logs.py) | `config: SilverWellLogsConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L149, fn=L158 |
| `silver_xlsx` | silver / — | [src/dagster/georag_dagster/assets/silver_xlsx.py](../../../src/dagster/georag_dagster/assets/silver_xlsx.py) | `config: SilverXlsxConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L535, fn=L550 |
| `silver_xyz` | silver / — | [src/dagster/georag_dagster/assets/silver_xyz.py](../../../src/dagster/georag_dagster/assets/silver_xyz.py) | `config: SilverXyzConfig`<br>`postgres: PostgresResource`<br>`minio: S3Resource` | `MaterializeResult` | deco=L191, fn=L200 |
| `silver_assays_v2_nl_summary` | silver_nl_summaries / postgres | [src/dagster/georag_dagster/assets/silver_nl_summaries.py](../../../src/dagster/georag_dagster/assets/silver_nl_summaries.py) | `postgres: PostgresResource` | `MaterializeResult` | deco=L202, fn=L212 |
| `silver_collars_nl_summary` | silver_nl_summaries / postgres | [src/dagster/georag_dagster/assets/silver_nl_summaries.py](../../../src/dagster/georag_dagster/assets/silver_nl_summaries.py) | `postgres: PostgresResource` | `MaterializeResult` | deco=L438, fn=L446 |
| `silver_lithology_nl_summary` | silver_nl_summaries / postgres | [src/dagster/georag_dagster/assets/silver_nl_summaries.py](../../../src/dagster/georag_dagster/assets/silver_nl_summaries.py) | `postgres: PostgresResource` | `MaterializeResult` | deco=L321, fn=L329 |
| `smdi_deposits_refresh` | smdi / — | [src/dagster/georag_dagster/assets/smdi_deposits.py](../../../src/dagster/georag_dagster/assets/smdi_deposits.py) | `config: SmdiRefreshConfig`<br>`postgres: PostgresResource` | `MaterializeResult` | deco=L217, fn=L226 |
| `reranker_chunk_population` | — / — | [src/dagster/georag_dagster/assets/reranker_labels.py](../../../src/dagster/georag_dagster/assets/reranker_labels.py) | `postgres: PostgresResource` | `Output[pl.DataFrame]` | deco=L345, fn=L355 |
| `reranker_chunk_sample` | — / — | [src/dagster/georag_dagster/assets/reranker_labels.py](../../../src/dagster/georag_dagster/assets/reranker_labels.py) | `{'population': AssetIn('reranker_chunk_population')}` | `Output[pl.DataFrame]` | deco=L438, fn=L447 |
| `reranker_generated_queries` | — / — | [src/dagster/georag_dagster/assets/reranker_labels.py](../../../src/dagster/georag_dagster/assets/reranker_labels.py) | `{'sample': AssetIn('reranker_chunk_sample')}` | `Output[pl.DataFrame]` | deco=L534, fn=L544 |
| `reranker_label_dataset` | — / — | [src/dagster/georag_dagster/assets/reranker_labels.py](../../../src/dagster/georag_dagster/assets/reranker_labels.py) | `{'mined': AssetIn('reranker_mined_negatives')}` | `MaterializeResult` | deco=L914, fn=L923 |
| `reranker_mined_negatives` | — / — | [src/dagster/georag_dagster/assets/reranker_labels.py](../../../src/dagster/georag_dagster/assets/reranker_labels.py) | `{'sample': AssetIn('reranker_chunk_sample'), 'queries': AssetIn('reranker_generated_queries')}` | `Output[pl.DataFrame]` | deco=L751, fn=L764 |
