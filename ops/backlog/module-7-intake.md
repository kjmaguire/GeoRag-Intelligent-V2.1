# Module 7 (WebSocket / Reverb Events) — pre-approved intake items

Items flagged during Module 3 Phase B (Chunk 1) that are explicitly out of Module 3
scope and must be picked up by Module 7 at intake.

Authority: Module 3 Phase B Chunk 1 close-out 2026-04-20.

---

## Chunk 1 status (backend-only) — applied 2026-04-22

| Item | Status | Notes |
|------|--------|-------|
| Reverb broadcast context + event_seq/event_id (WS-02) | **Backend-side complete** | EventStamper wired in `queries.py`; all SSE frames (status/routing/delta/citation/completed/failed) carry `event_seq` (monotonic int), `event_id` (UUID4), `answer_run_id`, `trace_id` (None until M10). UI consumption (dedup + reconnect replay) in Chunk 4. |
| GET /v1/answer_runs/{id}/events replay endpoint (WS-03) | **Backend-side complete** | Redis ring buffer on `db=2`, key pattern `georag:answer_run_events:<uuid>`, TTL 3600s. RBAC: workspace-scope check via `silver.answer_runs`. Returns JSON array sorted ascending by `event_seq`. UI wiring in Chunk 4. |
| Evidence inspector endpoint (item 3 below) | Untouched — landed Module 6 Chunk 4a | `GET /v1/evidence/{evidence_id}` live. UI wiring in Chunk 2/3. |

---

## 1. `commit_ingestion_run` Reverb broadcast on `ingestion.progress`

> **Status: pending — not part of Chunks 1/2/3 scope** — intake item still open for a later chunk.

- **Raised:** 2026-04-20 (Module 3 Phase B Chunk 1 close-out — B9 + B10 complete)
- **Action:** The `commit_ingestion_run` Dagster asset (new in Chunk 1) emits
  post-increment `workspace_data_version` and `project_data_version` as
  materialization metadata on every successful committed run. Module 7 must
  consume these values and broadcast a final `ingestion.progress` Reverb event
  on the `ingestion.progress` channel.

**Metadata fields available in the Dagster materialization event** (from
`commit_ingestion_run.py` `MaterializeResult`):

  | Metadata key                | Type    | Description |
  |---|---|---|
  | `workspace_id`              | text    | UUID of the workspace that was committed |
  | `workspace_data_version`    | integer | Post-increment workspace data_version |
  | `project_data_versions`     | text    | Comma-separated `project_id=version` pairs |
  | `projects_bumped`           | integer | Count of projects whose data_version was bumped |
  | `data_version_bump_sec`     | float   | Wall time for the atomic data_version UPDATE (seconds) |
  | `post_ingest_tune_sec`      | float   | Wall time for CLUSTER+ANALYZE+MV refresh (seconds) |

**Reverb event shape** (proposed — Module 7 owns the final shape):

  ```json
  {
    "event": "ingestion.committed",
    "channel": "ingestion.progress",
    "workspace_id": "<uuid>",
    "workspace_data_version": 1,
    "project_data_versions": [
      { "project_id": "<uuid>", "data_version": 1 }
    ],
    "committed_at": "<ISO-8601 UTC>"
  }
  ```

- **Trigger mechanism:** Module 7 should listen to Dagster's materialization
  event stream for `commit_ingestion_run` asset materializations. Options:
  1. A Dagster sensor in `definitions.py` that watches for the
     `commit_ingestion_run` materialization and enqueues a Laravel job.
  2. A Dagster resource hook that directly posts to a Laravel queue or
     webhook endpoint on successful materialization.
  3. Laravel Horizon polls Dagster's GraphQL API for recent
     `AssetMaterializationPlanned` events on `commit_ingestion_run`.
  Option 1 (Dagster sensor → Laravel queue) is recommended — keeps
  the broadcast logic in Laravel (Module 7's domain) and avoids coupling
  Dagster to Reverb's internal WebSocket channels.

- **Cache-key propagation:** Module 4 (retrieval cache) must consume
  `workspace_data_version` to invalidate stale cache entries per addendum §05d.
  Module 4 reads the version from PostgreSQL directly (`silver.workspaces`),
  not from the Reverb event — Module 7 broadcast is for real-time UI
  notification only, not for cache invalidation.

- **Pre-conditions before Module 7 can implement:**
  - `commit_ingestion_run` asset is live (done — Chunk 1, 2026-04-20)
  - `silver.workspaces.data_version` and `silver.projects.data_version` columns
    exist and have the monotonic trigger (done — Phase B1+B2, 2026-04-20)
  - Laravel Reverb channel `ingestion.progress` is defined (Module 7 owns)

- **Owner:** Module 7 (backend-laravel agent for the broadcast; data-engineer
  agent for any Dagster sensor addition needed to trigger the queue job).

---

---

## 2. Structured refusal payload (B4) — Module 6 Chunk 4a — FULLY CONSUMED 2026-04-22

> **Status: fully consumed; UI surface active** — Module 7 Chunk 3 (2026-04-22)
> `RefusalPanel` component live at `resources/js/Components/chat/RefusalPanel.tsx`.
> All 6 reason_codes mapped to header text. `searched` + `missing` + `failed_guards` blocks rendered.
> Nearest candidates are clickable → routes to EvidenceInspector. `failed` events synthesise
> a minimal RefusalPayload (`llm_unavailable` / `budget_exhausted`).
> Report-refusal-issue button present; Chunk 4 wires real feedback routing.

- **Raised:** 2026-04-22 (Module 6 Phase B Chunk 4a close-out)
- **Delivered by:** `backend-fastapi` agent

Module 6 now emits a full structured refusal payload (spec B4) in the SSE `completed` event when the citation guards reject an answer or the LLM backend fails. The payload is in `GeoRAGResponse.refusal_payload`.

**Shape:**
```json
{
  "type": "refusal",
  "reason_code": "guard_numeric_fail",
  "searched": {
    "stores_queried": ["neo4j", "postgis", "qdrant"],
    "candidates_considered": 12,
    "query_class": "factual"
  },
  "missing": {
    "what_was_needed": "Verified numerical values in the corpus to ground: 12.5, 99.9",
    "nearest_candidates": [
      {"marker": "[QDRANT:1]", "source_store": "qdrant", "relevance_score": 0.73, "preview": "..."}
    ]
  },
  "message": "We can't answer this from your corpus...",
  "failed_guards": ["numeric"]
}
```

**reason_code values (stable — Module 7 UI branches on these):**
- `guard_numeric_fail` — numeric guard rejected ungrounded numbers
- `guard_entity_fail` — entity guard rejected unresolved entities
- `guard_completeness_fail` — completeness guard found uncited sentences
- `insufficient_evidence` — 0 markers resolved, no guard fired
- `llm_unavailable` — all LLM backends exhausted (FB-02)
- `budget_exhausted` — TIMEOUT_GATHER_S exceeded

**How Module 7 consumes it:**
1. Check `completed` SSE event payload: if `refusal_payload is not None`, render refusal UI (not success UI)
2. Branch on `refusal_payload.reason_code` for the specific refusal message/suggestions
3. Show `refusal_payload.searched` block to indicate what was searched (corpus coverage)
4. Show `refusal_payload.missing.nearest_candidates` as "closest results found" chip list

**Laravel note:** The `refusal_payload` field is part of `GeoRAGResponse.model_dump()` which is serialised as the `completed` SSE event data. No Laravel changes needed for transport — it already relays the `completed` event to Reverb. Laravel may optionally persist `refusal_payload` to `query_audit_log` for analytics.

---

## 3. Evidence inspector endpoint (B6) — Module 6 Chunk 4a — RESOLVED 2026-04-22

- **Raised:** 2026-04-22 (Module 6 Phase B Chunk 4a close-out)
- **Resolved:** 2026-04-22 in Module 7 Chunk 2 — `EvidenceInspector` Sheet component wired to `GET /v1/evidence/{evidence_id}`, 4 type branches (document_passage/structured_record/graph_edge/map_feature), legacy SSE fallback path.
- **Delivered by:** `backend-fastapi` agent (endpoint); `frontend-engineer` (UI)

`GET /v1/evidence/{evidence_id}` is now live. Endpoint returns a type-branched payload for the Module 7 citation chip inspector panel.

**Auth:** X-Service-Key header required (same as all FastAPI internal endpoints). X-Workspace-Id header for workspace scope (UUID). Module 9 will harden with JWT workspace claim.

**Response branches by `evidence_type`:**

| evidence_type | Key fields |
|---|---|
| `document_passage` | `passage_text`, `context_before`, `context_after`, `deep_link`, `page`, `source_uri` |
| `structured_record` | `structured_ref`, `lineage`, `bronze_uri`, `parser_name`, `parser_version` |
| `graph_edge` | `graph_edge_ref`, `start_node_labels`, `start_node_preview`, `end_node_labels`, `end_node_preview`, `described_in` |
| `map_feature` | `map_feature_ref`, `tile_function`, `bbox`, `feature_properties` |

**How Module 7 consumes it:**
1. On citation chip click: `GET /v1/evidence/{evidence_id}` (can use Laravel as proxy or call FastAPI directly)
2. Branch on `evidence_type` to render the correct inspector panel:
   - `document_passage` → render passage text with highlighted context; link `deep_link` to document viewer
   - `structured_record` → render structured_ref table (schema/table/pk) + lineage provenance
   - `graph_edge` → render node labels + property preview for start/end nodes; list `described_in` documents
   - `map_feature` → render tile_function + bbox map preview chip; pass to MapLibre for feature highlight

**Error codes:**
- `404 {"detail": "Evidence not found"}` — not found OR cross-tenant (silent)
- `500 {"detail": "evidence_fetch_failed"}` — internal DB/Neo4j error (logged with evidence_id)

**Deep-link pattern (document_passage):**
`/api/v1/documents/view?bronze_uri=<uri>&page=<page>` — Module 7 should have or create a route for this. FastAPI sets it on `deep_link`; client can construct it from `source_uri` + `page` if preferred.

- **Owner:** Module 7 (frontend-engineer for inspector UI; backend-laravel for proxy route if needed)

---

*Append new items as Module 3/4/5/6 work surfaces additional Module 7 dependencies.*
*Do not close items here — close them in Module 7 Phase A intake verification.*
