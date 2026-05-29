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

Set in `.env` and propagated to all containers via `docker-compose.yml`:

| Variable | Example value | Notes |
|----------|--------------|-------|
| `AWS_ACCESS_KEY_ID` | `georag_minio_user` | SeaweedFS S3 access key |
| `AWS_SECRET_ACCESS_KEY` | `georag_minio_password` | SeaweedFS S3 secret key |
| `AWS_ENDPOINT_URL` | `http://minio:8333` | Docker-internal hostname; use `http://localhost:8333` from host |
| `AWS_DEFAULT_REGION` | `us-east-1` | Required by boto3/SDK; SeaweedFS accepts any value |
| `S3_ENDPOINT_URL` | `http://minio:8333` | Alias used by backup-agent scripts; keep in sync with `AWS_ENDPOINT_URL` |

**Hostname note:** `minio` resolves to the SeaweedFS container on the `georag` Docker network.
From the host machine or any external client, use `http://localhost:8333`.

---

## How to Construct a Client

### Python (FastAPI / Dagster)

```python
import boto3
import os

def get_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["AWS_ENDPOINT_URL"],
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )

# Usage:
s3 = get_s3_client()
s3.put_object(Bucket="georag-bronze", Key="raw/report.pdf", Body=file_bytes)
obj = s3.get_object(Bucket="georag-bronze", Key="raw/report.pdf")
s3.head_bucket(Bucket="georag-bronze")  # Check existence (raises if missing)
```

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

Expected output:
```
[PASS] AWS_ENDPOINT_URL set: http://minio:8333
[PASS] S3 endpoint reachable
[PASS] Bucket georag-backups exists
[PASS] Bucket georag-bronze exists
[PASS] Bucket georag-exports exists
[PASS] Write/read/delete round-trip OK
[PASS] No vendor SDK imports found in app/ or src/
S3 abstraction check: 7/7 PASSED
```

---

## Anti-Patterns

These patterns are **prohibited** by addendum §02a:

| Anti-pattern | Why prohibited | Correct alternative |
|-------------|---------------|---------------------|
| `from minio import Minio` | Vendor SDK; ties code to MinIO/SeaweedFS API | `import boto3` |
| `client.fput_object(bucket, key, path)` | MinIO-specific method | `s3.put_object(Bucket=b, Key=k, Body=data)` |
| `client.bucket_exists(name)` | MinIO-specific method | `s3.head_bucket(Bucket=name)` (catch exception) |
| Hardcoded endpoint `http://localhost:8333` | Breaks in container | `os.environ["AWS_ENDPOINT_URL"]` |
| Hardcoded bucket name `"georag-bronze"` | Breaks on rename | `os.environ["BRONZE_BUCKET"]` or config constant |
| SeaweedFS admin API calls (`/cluster/status`) | Vendor-specific; not part of S3 contract | N/A — remove from application code |
| AWS-specific SDK features (Glacier, SQS, SNS) | AWS-only; not available on SeaweedFS | Use generic S3 primitives only |

**Known violation to fix in Module 3:** `src/dagster/georag_dagster/resources.py` imports
`from minio import Minio` and uses `fput_object`, `bucket_exists`, `make_bucket`. Replace with
boto3 before Module 3 Phase B ingestion code runs. Pre-approved in `ops/backlog/module-3-intake.md`.

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
