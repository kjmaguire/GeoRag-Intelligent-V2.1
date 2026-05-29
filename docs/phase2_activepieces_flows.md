# Phase 2 — Activepieces flow definitions (manual setup runbook)

**Status:** Active. Phase 2 keeps Activepieces flows authoritative in
its own DB (no flow-as-code export yet — that's Phase 3+). This doc is
the **operator runbook** for re-creating the canonical flows in a fresh
environment.

---

## Why this doc exists

The `bronze_public_geoscience` Dagster asset stays untouched through
Phase 2. The new path is a thin parallel slice:

```
[ Activepieces cron ]
        │  HTTP GET upstream
        ▼
[ S3 (bronze bucket) ]   key: public_geoscience/<source_id>/<ts>.geojson
        │  HTTP POST {minio_key, source_id, source_url, fetched_at}
        │  X-Service-Key: $FASTAPI_SERVICE_KEY
        ▼
[ FastAPI /internal/v1/integrations/public_geoscience_pull/trigger ]
        │  aio_run_no_wait(...)
        ▼
[ Hatchet workflow public_geoscience_pull ]
        ├─ feature-flag gate
        ├─ S3 GET → validate GeoJSON → SHA256
        ├─ INSERT bronze.provenance
        └─ emit_audit('public_geoscience.pull.complete')
```

Everything to the right of "[ S3 ]" is in code. Everything above is
defined in Activepieces' own UI/DB. This doc captures the flow shape.

---

## Flow #1 — `public_geoscience_pull` (Phase 2 Step 4)

### One-time prerequisites

In Activepieces (http://localhost:8090):

1. **App connection: `georag_internal_api`**
   - Type: HTTP / Custom
   - Auth: header `X-Service-Key` with the value of
     `FASTAPI_SERVICE_KEY` (read from `.env`).
   - Base URL: `http://fastapi:8000` (Docker network) or
     `http://host.docker.internal:8000` from outside compose.

2. **App connection: `georag_bronze_s3`**
   - Type: AWS S3 / S3-compatible
   - Endpoint: `http://minio:8333`
   - Region: `us-east-1`
   - Access key + secret: from `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`.

### Flow steps

| # | Piece | Config |
|---|-------|--------|
| 1 | **Schedule trigger** | Cron `0 */6 * * *` (every 6 hours, configurable per source). Phase 2 default is 6h; bump down if upstream is more dynamic. |
| 2 | **HTTP — GET** | URL: `https://<upstream-feed>/MapServer/<layer>/query?where=1=1&outFields=*&f=geojson` (specific upstream is per-source; Phase 2 starts with one configured layer). Capture response body as `{{step_2.body}}`. |
| 3 | **AWS S3 — Put Object** | Bucket: `bronze`. Key: `public_geoscience/{{flow.run.id}}/{{currentTimeISO}}.geojson`. Body: `{{step_2.body}}`. Content-Type: `application/geo+json`. |
| 4 | **HTTP — POST** | URL: `{{connections.georag_internal_api.baseUrl}}/internal/v1/integrations/public_geoscience_pull/trigger`. Headers: `X-Service-Key: {{connections.georag_internal_api.serviceKey}}`. Body: `{ "minio_key": "{{step_3.key}}", "source_id": "<configured-source-id>", "source_url": "<step-2-url>", "fetched_at": "{{currentTimeISO}}" }`. |

### Activation gate

The Hatchet-side workflow is gated on the platform feature flag:

```sql
INSERT INTO workspace.feature_flags
    (workspace_id, flag_name, bool_value, updated_at)
VALUES (NULL, 'activepieces.public_geoscience_pull.enabled', true, now())
ON CONFLICT (workspace_id, flag_name) DO UPDATE
    SET bool_value = true, updated_at = now();
```

Or via the dashboard at `/admin/integrations` (Step 6). When `false`,
the workflow short-circuits with `skipped=true` — Activepieces still
fetches + stores in S3 (we don't waste the upstream call), but no
bronze.provenance row is written.

### Verification

After enabling the flag and waiting for the first cron tick:

```sql
-- Most recent successful pull
SELECT ingested_at,
       source_file,
       source_file_sha256,
       (source_col_map->>'source_id')   AS source_id,
       (source_col_map->>'feature_count')::int AS features
  FROM bronze.provenance
 WHERE parser_name = 'activepieces_public_geoscience_pull'
 ORDER BY ingested_at DESC
 LIMIT 5;
```

The corresponding audit row:

```sql
SELECT created_at, payload->>'source_id' AS source_id,
       payload->>'feature_count' AS features
  FROM audit.audit_ledger
 WHERE action_type = 'public_geoscience.pull.complete'
 ORDER BY created_at DESC LIMIT 5;
```

### Idempotency

The Hatchet workflow checks `bronze.provenance` for an existing row
with the same `source_file_sha256` + `parser_name` and skips the
INSERT if it already exists. If Activepieces runs the flow twice
against an unchanged upstream, you get one provenance row + two audit
entries (one with `idempotent_skip=true` in the payload).

### Pause / rollback

Three reversal paths, in order of escalation:

| Step | What | Effect |
|------|------|--------|
| 1 | `UPDATE feature_flags SET bool_value=false …` | Hatchet workflow short-circuits; Activepieces still fetches, but nothing lands in bronze.provenance. |
| 2 | Disable the flow in Activepieces UI | No more cron ticks; nothing fetched at all. |
| 3 | Delete the flow | Same as 2 + can't re-enable without re-creating per this doc. |

---

## Flow #2 — `external_notification` (Phase 2 Step 5a — webhook receiver)

Inbound webhook bridge. External senders POST a JSON envelope; the
flow forwards it to FastAPI; the Hatchet workflow records it in
`audit.audit_ledger` with idempotency keyed on `notification_id`.

### One-time prerequisites

Reuse the `georag_internal_api` connection from Flow #1.

### Flow steps

| # | Piece | Config |
|---|-------|--------|
| 1 | **Webhook trigger** | URL is generated by Activepieces; copy it after saving the flow. Issue this URL (with the secret query param Activepieces appends) to external senders. |
| 2 | **Code piece (TS) — shape the envelope** | Map the inbound payload to `{notification_id, source, kind, payload, received_at}`. The first three are required; if the sender doesn't supply `notification_id`, generate one (e.g. SHA-256 of payload + timestamp) — you LOSE idempotency if you do this, so prefer that the sender sets it. |
| 3 | **HTTP — POST** | URL: `{{connections.georag_internal_api.baseUrl}}/internal/v1/integrations/external_notification/trigger`. Headers: `X-Service-Key: {{connections.georag_internal_api.serviceKey}}`. Body: the shaped envelope from step 2. |

### Activation gate

```sql
INSERT INTO workspace.feature_flags
    (workspace_id, flag_name, bool_value, updated_at)
VALUES (NULL, 'activepieces.external_notification.enabled', true, now())
ON CONFLICT (workspace_id, flag_name) DO UPDATE
    SET bool_value = true, updated_at = now();
```

When the flag is `false`, Activepieces still accepts the webhook
(returning 200 to the external sender), but the Hatchet workflow
short-circuits with `skipped=true` and writes nothing to
`audit.audit_ledger`.

### Verification

```sql
-- Recent inbound notifications
SELECT created_at,
       payload->>'source'          AS source,
       payload->>'kind'            AS kind,
       payload->>'notification_id' AS notification_id
  FROM audit.audit_ledger
 WHERE action_type = 'external_notification.received'
 ORDER BY created_at DESC LIMIT 20;
```

### Idempotency

The Hatchet workflow checks `audit.audit_ledger` for an existing row
with the same `payload->>'notification_id'` before emitting. Re-deliveries
of the same `notification_id` return early with `skipped=true,
reason="duplicate notification_id"` and do NOT add a second audit row.
Senders that retry on transient failures get the same effect as exactly-once
delivery without the protocol overhead.

### Sender authentication

Activepieces' webhook trigger supports a query-string secret +
header-based auth out of the box. Phase 2 expects that to be sufficient
(operator configures per-sender). HMAC-with-shared-secret is a Phase 3
hardening item — for Phase 2 the trust boundary is at the
Activepieces-managed webhook URL.

---

## Phase 3 deferred — flow-as-code

Phase 2 keeps flow definitions in Activepieces' own DB. Phase 3 is
expected to add an export-to-JSON-and-commit pattern + a CI gate so
flow edits go through code review. Until then, Activepieces' UI is
authoritative and this doc is the recreation runbook.
