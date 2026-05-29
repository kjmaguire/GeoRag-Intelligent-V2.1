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
