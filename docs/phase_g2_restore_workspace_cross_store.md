# Phase G.2 — `restore_workspace` cross-store consistency dry-run

**Status:** Complete. Master-plan §11.3 deliverable "Cross-store
consistency restore tested" partially closes (dry-run path); real
restore (dry_run=False) still gates on §11.1 backup infrastructure.

## What changed

`src/fastapi/app/hatchet_workflows/restore_workspace.py` —
dry-run path extended from "count PG silver tables only" to **count
all five stores + verify a snapshot manifest**:

| Store | What's counted | Implementation |
|---|---|---|
| **PostgreSQL** | 9 tables: `silver.workspaces`, `silver.hypotheses`, `silver.decision_records`, `audit.audit_ledger`, `ops.support_tickets`, `silver.answer_runs`, `silver.evidence_items`, `silver.document_passages`, `targeting.target_recommendations` | `_count_postgres_rows` — per-table COUNT(*) with the workspace_id column |
| **Neo4j** | Nodes carrying the workspace_id (direct or via Project node lookup) | `_count_neo4j_nodes` — async driver, parameterised cypher with graceful fallback when the project-traverse path errors |
| **Qdrant** | Points in `georag_reports` whose payload `workspace_id` matches | `_count_qdrant_points` — `client.count(filter=workspace_id, exact=True)` |
| **Redis** | Keys under `georag:ws:<ws-id>:*` | `_count_redis_keys` — SCAN iter (best-effort; Redis isn't authoritative) |
| **SeaweedFS** | (planned — Phase 11.1) | Deferred alongside real restore |

Each collector returns `(count, error_or_none)`. A failed collector
gives `count=-1` so the consistency-check result still ships the
other stores' values + an `errors` map for operators to inspect.

### Snapshot manifest probe

New `_verify_snapshot_manifest(manifest_uri, live_counts)` helper:

* Reads a `file://` URI (S3 deferred to Phase 11.1) carrying a v1
  schema JSON with per-store row counts + workspace_id.
* Compares manifest's claimed counts to live counts; surfaces a
  `mismatches` list with `{store, key, expected, actual}` per
  divergence. Stores with `actual=-1` (collector failed) are skipped
  rather than flagged.
* Verifies the manifest's `workspace_id` matches the workspace we're
  restoring.

### Audit anchor

Payload now carries the full `live_counts` dict + the
`manifest_mismatches` count + any `store_errors`. Operators can
read this from `audit.audit_ledger` post-hoc to verify which stores
were healthy at restore-precheck time.

### Output model

`RestoreWorkspaceOutput.consistency_check_results` shape (v2):

```json
{
    "workspace_name": "Cameco Shirley Basin Uranium",
    "workspace_slug": "cameco-shirley-basin",
    "manifest_uri": "file:///snapshots/ws-abc/2026-05-15.json",
    "snapshot_verified": true,
    "live_counts": {
        "workspace_id": "<uuid>",
        "postgres": { "silver_workspaces": 1, "silver_decision_records": 5, ... },
        "neo4j_nodes": 1108,
        "qdrant_points": 1108,
        "redis_keys": 0
    },
    "store_errors": {},
    "manifest_check": {
        "loaded": true,
        "manifest_version": "1.0",
        "captured_at": "2026-05-13T00:00:00Z",
        "matches_workspace_id": true,
        "mismatches": []
    },
    "total_rows_in_workspace": 6
}
```

## Tests

`src/fastapi/tests/test_restore_workspace_cross_store.py` — **8 tests**:

* Manifest unsupported scheme (s3://) → `loaded=False`
* Manifest missing file → `loaded=False`
* Manifest invalid JSON → `loaded=False`
* All counts match → 0 mismatches
* Some counts mismatch → per-store mismatch records
* Failed collector (-1) is skipped, not flagged
* `dry_run` default is True (safety contract)
* `snapshot_manifest_uri` is required (no silent default)

Canary post-G.2: **219 / 0** (+8 new, baseline holds since G.1).

## Worker registration

`worker.py` comment updated: `restore_workspace` no longer labelled
"Skeleton" — dry-run path is now production-ready; real-restore
remains explicitly gated.

## What this enables

Operators can now invoke:

```
hatchet trigger restore_workspace \
    --workspace-id <uuid> \
    --snapshot-manifest-uri file:///snapshots/ws/2026-05-13.json \
    --initiated-by-user-id 1 \
    --restore-request-id <uuid>
```

…and get a one-page "is my workspace healthy across all stores"
report in the audit ledger, plus a list of any mismatches against
the snapshot manifest. That's the DR-readiness signal the master
plan §11 calls for; the actual `pg_restore` / `neo4j-admin restore`
path is now the only thing standing between this and a true restore.

## Carry-overs

* **Real restore body** (`dry_run=False`) still raises a clean
  failure. Wiring it requires:
  * Snapshot manifest verification (signed by backup-agent)
  * `pg_restore --schema=silver,audit,workspace,...` orchestration
  * `neo4j-admin database restore` per workspace's database
  * Qdrant snapshot API restore call
  * Redis `DEBUG RELOAD` or AOF replay
  * SeaweedFS object replication (per ADR-0001)
  All five are Phase 11.1 deliverables.
* **S3 manifest scheme** — `s3://` URIs in the manifest_uri input
  are surfaced as `loaded=False` with a "scheme not yet supported"
  reason. Real production manifests will live in SeaweedFS so this
  has to ship with Phase 11.1.
* **SeaweedFS object count** — not yet collected. Add a
  `_count_seaweedfs_objects` helper using the S3 SDK when bucket
  naming convention firms up.
* **Admin trigger UI** — the workflow is invokable via the Hatchet
  CLI + `/admin/hatchet-workers` (workflow trigger panel) but doesn't
  have its own dedicated `/admin/dr-readiness` page yet. Deferred —
  the audit-ledger payload is operationally sufficient.
