# S3 Abstraction Runbook
<!-- What: Addendum §02a contract, client construction, env vars, implementation swap, integrity test, anti-patterns -->
<!-- When: Before writing any code that reads from or writes to object storage; before swapping S3 backends -->
<!-- Authority: georag-architecture.html addendum §02a; ops/audit/2026-04-19-datastores-audit.md A6 -->
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-20 (Module 2 Phase D) -->

---

## The Contract (Addendum §02a)

All GeoRAG application code must access object storage through a vendor-neutral S3 interface.
Three rules apply:

1. **Use boto3 (Python) or the Laravel AWS SDK (PHP)** with `endpoint_url` from environment.
   Never import vendor-specific SDKs (`minio`, `seaweedfs-client`, or any SDK with a vendor name).

2. **Read endpoint and credentials from environment variables.** Never hardcode an endpoint,
   access key, or bucket name in application code.

3. **Use only S3-compatible API operations.** Standard operations: `put_object`, `get_object`,
   `delete_object`, `list_objects_v2`, `create_bucket`, `head_bucket`. Do not use vendor-specific
   admin APIs (SeaweedFS `/cluster/status`, MinIO `/minio/health/live`, etc.).

The current S3 backend is SeaweedFS 4.20, replacing MinIO per ADR-0001. The application cannot
tell the difference — the endpoint and credentials are injected via env, and all operations are
standard S3.

---

## Required Environment Variables

Set in `.env` and propagated to all containers via `docker-compose.yml`.
Canonical names (storage-abstraction plan, 2026-07 — resolved by
`georag_object_storage.StorageConfig.from_env()`):

| Variable | Example value | Notes |
|----------|--------------|-------|
| `AWS_ACCESS_KEY_ID` | `georag_minio_user` | SeaweedFS S3 access key |
| `AWS_SECRET_ACCESS_KEY` | `georag_minio_password` | SeaweedFS S3 secret key |
| `AWS_ENDPOINT_URL` | `http://minio:8333` | **The single endpoint source of truth.** Docker-internal hostname; use `http://localhost:8333` from host |
| `AWS_DEFAULT_REGION` | `us-east-1` | Required by boto3/SDK; SeaweedFS accepts any value |
| `AWS_BUCKET_BRONZE` / `AWS_BUCKET_EXPORTS` / `AWS_BUCKET_BACKUPS` / `AWS_BUCKET_BRONZE_RASTER` | `bronze` etc. | Logical-bucket overrides; defaults match today's live names |

The earlier revision of this runbook required `S3_ENDPOINT_URL` to be "kept
in sync" with `AWS_ENDPOINT_URL`. That two-source-of-truth contract was
itself a source of drift (it's how the backup workflows ended up pointed at
a dead port — see `docker-compose.yml`'s `SEAWEEDFS_S3_ENDPOINT` default)
and is retired: **set only `AWS_ENDPOINT_URL`**. Every legacy name
(`S3_ENDPOINT_URL`, `S3_ENDPOINT`, `MINIO_*`, `SEAWEEDFS_*`,
`SEAWEEDFS_S3_*`) is still read as a documented fallback by
`StorageConfig.from_env()` — with a one-time warning log — so existing
`.env` files keep working during the transition, but new deployments and
docs should use canonical names only.

**Hostname note:** `minio` resolves to the SeaweedFS container on the `georag` Docker network.
From the host machine or any external client, use `http://localhost:8333`.

---

## How to Construct a Client

### Python (FastAPI / Dagster)

Do NOT construct boto3/aioboto3 clients inline. Use the shared
`georag_object_storage` package (`src/georag_object_storage/`, installed
into both services as a path dependency):

```python
from georag_object_storage import Bucket, StorageConfig, get_storage_client
from georag_object_storage.async_client import AsyncS3CompatibleStorage

# Sync (Dagster, scripts):
store = get_storage_client()                      # reads env via StorageConfig.from_env()
store.put_bytes(Bucket.BRONZE, "raw/report.pdf", file_bytes)
pdf = store.get_bytes(Bucket.BRONZE, "raw/report.pdf")

# Async (FastAPI, Hatchet workflows — never block the event loop):
storage = AsyncS3CompatibleStorage(StorageConfig.from_env())
pdf = await storage.get_bytes(Bucket.BRONZE, "raw/report.pdf")
```

Escape hatch for genuinely dynamic bucket names (backup snapshot targets,
outbox rows, tier-{name} buckets) — share the client construction, keep the
raw API:

```python
from georag_object_storage import StorageConfig, async_client_kwargs, build_boto3_client

s3 = build_boto3_client(StorageConfig.from_env())          # sync
async with aioboto3.Session().client("s3", **async_client_kwargs(StorageConfig.from_env())) as s3:
    ...                                                     # async
```

The `STORAGE_BACKEND` env var (default `s3_compatible`) is the deliberate
seam for a future Azure Blob backend; any other value currently raises
`NotImplementedError`.

### PHP (Laravel)

Laravel's AWS SDK reads `AWS_*` env vars automatically when `AWS_ENDPOINT` is set:

```php
// config/filesystems.php — already configured if the env vars are present
's3' => [
    'driver' => 's3',
    'key'    => env('AWS_ACCESS_KEY_ID'),
    'secret' => env('AWS_SECRET_ACCESS_KEY'),
    'region' => env('AWS_DEFAULT_REGION', 'us-east-1'),
    'bucket' => env('AWS_BUCKET', 'georag-bronze'),
    'url'    => env('AWS_ENDPOINT'),
    'endpoint' => env('AWS_ENDPOINT'),
    'use_path_style_endpoint' => true,  // Required for SeaweedFS / non-AWS S3
],
```

Usage:
```php
Storage::disk('s3')->put('raw/report.pdf', $fileContents);
$url = Storage::disk('s3')->url('raw/report.pdf');
```

### Bash (backup scripts)

```bash
aws s3 cp /local/file.tar.gz s3://georag-backups/postgres/file.tar.gz \
  --endpoint-url "$S3_ENDPOINT_URL"

aws s3 ls s3://georag-bronze/ --endpoint-url "$S3_ENDPOINT_URL"
```

---

## How to Swap S3 Implementations

To replace SeaweedFS with any S3-compatible backend (MinIO, Ceph RGW, Backblaze B2, Wasabi,
AWS S3):

1. Update `.env`:
   ```
   AWS_ACCESS_KEY_ID=<new-key>
   AWS_SECRET_ACCESS_KEY=<new-secret>
   AWS_ENDPOINT_URL=<new-endpoint>
   S3_ENDPOINT_URL=<new-endpoint>
   ```

2. Update `docker-compose.yml` to remove or replace the `georag-minio` service definition if
   the backend is external.

3. Recreate all containers that read `AWS_*` env vars:
   ```bash
   docker compose up -d --force-recreate georag-backup-agent georag-fastapi \
     georag-laravel-octane georag-laravel-horizon georag-dagster-daemon
   ```

4. Run the integrity test (see below) to confirm the new backend is reachable and bucket-complete.

5. No application code changes are required, provided the new backend is S3-API-compatible.

---

## Integrity Test

Location: `ops/tests/s3-abstraction-check.sh`

The test script verifies:
1. All required env vars are set
2. The endpoint is reachable (`aws s3 ls --endpoint-url ...` exits 0)
3. All required buckets exist (`georag-backups`, `georag-bronze`, `georag-exports`)
4. A write+read+delete round-trip succeeds on `georag-bronze`
5. No vendor-specific SDK imports exist in application code (`grep` for `minio`, `seaweedfs`)

Run:
```bash
# From host:
bash ops/tests/s3-abstraction-check.sh

# From backup-agent container (uses container-internal endpoint):
docker exec georag-backup-agent bash /tests/s3-abstraction-check.sh
```

The script runs 7 steps (aligned with the script as of the 2026-07
storage-abstraction pass — earlier revisions of this runbook described
checks the script never implemented):

1. PUT a timestamped test object
2. GET it back and verify content matches
3. HEAD the object (metadata retrievable)
4. LIST the prefix and confirm the key appears
5. DELETE the object
6. Verify deletion (GET now 404s)
7. Grep application source (`src/fastapi`, `src/dagster`,
   `src/georag_object_storage`) for vendor-SDK imports (`minio`,
   `seaweedfs`) — skipped with a log line when no source checkout is
   present (e.g. run from a bare aws-cli container)

Exit 0 = all steps passed.

---

## Anti-Patterns

These patterns are **prohibited** by addendum §02a:

| Anti-pattern | Why prohibited | Correct alternative |
|-------------|---------------|---------------------|
| `from minio import Minio` | Vendor SDK; ties code to MinIO/SeaweedFS API | `georag_object_storage` (or raw boto3 via its `build_boto3_client`) |
| `client.fput_object(bucket, key, path)` | MinIO-specific method | `store.put_file(Bucket.BRONZE, key, path)` |
| `client.bucket_exists(name)` | MinIO-specific method | `store.bucket_exists(Bucket.BRONZE)` |
| Inline `boto3.client("s3", endpoint_url=os.environ[...])` construction | Duplicated env resolution — the drift this runbook exists to prevent | `StorageConfig.from_env()` + package clients |
| Hardcoded endpoint `http://localhost:8333` | Breaks in container | `AWS_ENDPOINT_URL` via `StorageConfig.from_env()` |
| Hardcoded bucket name `"georag-bronze"` | Breaks on rename | `Bucket` enum + `AWS_BUCKET_*` overrides |
| SeaweedFS admin API calls (`/cluster/status`) | Vendor-specific; not part of S3 contract | N/A — remove from application code |
| AWS-specific SDK features (Glacier, SQS, SNS) | AWS-only; not available on SeaweedFS | Use generic S3 primitives only |

**Resolved.** `src/dagster/georag_dagster/resources.py`'s `S3Resource` has used boto3 since
Module 3 — the violation this note used to describe. The storage-abstraction plan's PR2 (2026-07)
found and fixed the one remaining violation: `src/fastapi/scripts/populate_source_files.py`
imported `from minio import Minio` directly. `ops/tests/s3-abstraction-check.sh` now has a step
that greps for this class of violation on every run, so a new one won't go unnoticed again.

---

## Bucket Naming Decision Pending

Architecture addendum §02b specifies bucket names `bronze` and `bronze-raster`. Live buckets are
named `georag-bronze` and `georag-exports`. This naming drift is tracked in
`ops/backlog/module-10-doc-sweep.md` ("SeaweedFS Bucket Naming"). The bucket name used in
application code must match whatever is decided before Module 3 ingestion begins.

See `ops/backlog/module-10-doc-sweep.md` for the three resolution options (rename live, update
addendum, or additive migration).

---

## Provenance

- Date: 2026-04-20
- Module: 2 Phase D
- Produced by: devops-engineer agent (Claude Sonnet 4.6)
- Authority: georag-architecture.html addendum §02a; ADR-0001 (SeaweedFS replaces MinIO)
- Integrity test: ops/tests/s3-abstraction-check.sh
