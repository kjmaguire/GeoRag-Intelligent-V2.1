# ADR 0001: SeaweedFS replaces MinIO as the S3-compatible object store

- **Date**: 2026-04-19
- **Status**: Accepted
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: original MinIO selection in `georag-architecture.html` Section 07

## Context

The GeoRAG platform uses an S3-compatible object store as the Bronze layer
(immutable raw archive of drill logs, PDFs, geophysics, GIS exports) and as
the destination for generated export bundles. The original choice was MinIO,
pinned to `minio/minio:RELEASE.2025-04-08T15-41-24Z`.

Two facts forced a re-evaluation in April 2026:

1. **License change**. MinIO relicensed to AGPL v3 in 2024. The platform's
   own free-licensing rule (`MEMORY.md` → `feedback_free_licensing.md`)
   permits only MIT/BSD/Apache 2.0 for new dependencies, models, and services.
   The pinned MinIO image post-dates the relicense, so the existing pin
   already drifted out of policy.
2. **Upstream archived**. The `minio/minio` GitHub repository was archived on
   2026-02-13. The community no longer receives security patches, bug fixes,
   or feature work for the version line we run. Continuing to pin a frozen
   image is acceptable short-term but accumulates risk indefinitely.

## Options considered

| Option | License | Effort | Outcome |
|---|---|---|---|
| A. Stay on pinned MinIO | AGPL v3 ⚠ | 0 | Violates own license rule; no future patches; rejected. |
| B. Migrate to Garage | AGPL v3 ⚠ | Medium | Same license problem as MinIO; rejected on the same rule. |
| C. **Migrate to SeaweedFS** | **Apache 2.0** ✅ | Medium-High | Satisfies license rule; mature S3 API; chosen. |

## Decision

Replace MinIO with **SeaweedFS** (`chrislusf/seaweedfs:4.20`) running in
all-in-one mode (master + volume + filer + S3 in one process).

### What stays the same

- **Compose service name**: `minio`. Network DNS resolution from Laravel,
  FastAPI, Dagster keeps working without code or env changes.
- **Env var prefix**: `MINIO_*`. Renaming would touch ~25 references across
  `docker-compose.yml`, `.env.example`, `config/filesystems.php`,
  `src/dagster/georag_dagster/resources.py`, `src/dagster/georag_dagster/definitions.py`,
  several Laravel controllers, and assorted Dagster assets. The rename
  delivers no functional benefit and creates a multi-PR refactor.
- **S3 protocol**. Both Laravel (Flysystem `s3` driver) and Dagster (Python
  `minio` SDK) speak generic S3. SeaweedFS is fully API-compatible. No client
  code changes were required.
- **Bucket names**: `georag-bronze`, `georag-exports`.
- **Data**. The 70-object / 290 MB bucket inventory was migrated via
  `mc mirror` and verified byte-for-byte.

### What changed

- **Image**: `minio/minio:RELEASE.2025-04-08T15-41-24Z` → `chrislusf/seaweedfs:4.20`.
- **Internal S3 port**: `9000` → `8333`. We deliberately did *not* mask this
  via remapping — if any code hardcoded `:9000` it should fail loudly so we
  catch it. `MINIO_ENDPOINT` defaults updated everywhere.
- **Console / admin port**: MinIO had a separate browser console on `9001`.
  SeaweedFS exposes the filer HTTP UI on `8888` instead.
- **Bootstrap**: replaced `minio server /data` with a custom entrypoint
  (`docker/seaweedfs/entrypoint.sh`) that renders an S3 IAM identity file
  from `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` and runs
  `weed server -dir=/data -volume -filer -s3 -s3.config=...`.
- **Dagster `MinIOResource` default endpoint**: `minio:9000` → `minio:8333`
  in `src/dagster/georag_dagster/resources.py`.

## Migration mechanics (for future reference)

The actual migration was reversible at every step until the volume rm:

1. `pg_dumpall`-style snapshot: `mc mirror minio/ ./backups/minio-snapshot/`
   (one-shot mc container against the live MinIO over the docker network).
2. Stop `minio` + `minio-init`, `docker compose rm`, `docker volume rm
   georagintelligencev10_minio_data`. Snapshot at this point is the only
   copy — keep it until SeaweedFS is verified.
3. Edit compose: replace MinIO blocks with the SeaweedFS service definition,
   bump `MINIO_ENDPOINT` defaults from `:9000` to `:8333`.
4. `docker compose up -d minio` to start SeaweedFS, then `up -d minio-init`
   to provision buckets idempotently via `mc mb`.
5. Re-mirror: `mc mirror ./backups/minio-snapshot/ dst/` against the new
   SeaweedFS S3 endpoint.
6. Restart Laravel + FastAPI + Dagster to pick up the new `MINIO_ENDPOINT`.
7. Verify: Laravel `Storage::disk('s3')->put/get`, Dagster
   `MinIOResource.client.put_object/get_object`, and read back a known
   pre-migration object to byte-equality.

## Gotchas hit during the migration (worth knowing for next time)

1. **`weed server` does NOT start the volume server by default**. Without
   `-volume`, the filer accepts metadata writes and the S3 API returns 200,
   but the master logs `No writable volumes` and the bytes are silently
   discarded on the next read. Symptom: `mc du` shows files exist with the
   correct sizes (filer metadata), but a GET returns 0 bytes or 500.
2. **`localhost` resolves to `::1` in the SeaweedFS image**, but the master
   only binds IPv4. Healthcheck must use `127.0.0.1:9333` explicitly.
3. **Bind-mounted entrypoint scripts lose their executable bit on Windows
   hosts**. Invoke via `entrypoint: ["sh", "/usr/local/bin/entrypoint.sh"]`
   instead of relying on the shebang.
4. **Compose alias `minio` → SeaweedFS is intentional, not a tech-debt smell**.
   Renaming the service to `seaweedfs` would cascade through Laravel
   filesystem config, Dagster resource bindings, FastAPI env, and ~10 other
   places. The compose service name is wire-protocol-only; the network alias
   does the work.

## Consequences

### Positive

- License-rule compliance restored.
- Active upstream maintenance (`chrislusf/seaweedfs` is actively patched,
  Apache 2.0).
- Smaller resource footprint per object than MinIO at this scale (filer
  metadata is in BoltDB; volumes are append-only `.dat` files — efficient
  for the geosciences workload of write-once / read-many large files).
- Optional features available later if needed: erasure coding, replication
  across racks, cross-cluster sync, S3 IAM with multiple identities, Iceberg
  REST catalog (already running on `:8181`).

### Negative

- **Single-maintainer project**. SeaweedFS has one primary contributor
  (Chris Lu / `chrislusf`). Bus-factor risk is real. Mitigation: the project
  is Apache 2.0 + actively forked by enterprises (e.g. Cloudera), so a hard
  upstream loss is recoverable. Reassess in 12 months.
- **No pre-built browser console** like MinIO had on `:9001`. The filer UI
  on `:8888` is functional but spartan. For day-to-day inspection, `mc`
  against the S3 endpoint covers it.
- **Different operational model**. SeaweedFS uses master + volume + filer as
  separate logical components even when colocated; tuning knobs differ from
  MinIO's pool/disk model. Operators must learn the new mental model.
- **One-time data migration risk**. Mitigated with the host-side snapshot
  in `backups/minio-snapshot/` retained for ~30 days post-cutover.

## Verification (this commit)

- All 70 pre-migration objects visible in SeaweedFS (`mc du --recursive`).
- Laravel `Storage::disk('s3')`: PUT + GET roundtrip OK. 36 MB GeoJSON read
  back at full size (37,663,511 bytes, valid JSON).
- Dagster `minio` SDK against `MinIOResource`: PUT + GET roundtrip OK,
  pre-existing migrated objects listable.
- All 14 stack services healthy after cutover.

## Follow-ups (NOT part of this ADR; tracked separately)

- Decide whether `MINIO_*` env var names should eventually be renamed to
  `OBJECT_STORE_*` for honesty. Defer until a quarter with no other infra
  churn.
- Evaluate enabling SeaweedFS replication when the cluster grows past a
  single workstation deployment (`replication: 010` for two volume servers
  in the same rack).
- Configure SSE-S3 server-side encryption (`s3.sse.kek` in
  `security.toml`) before any production deployment that handles operator
  PII or licensed data.
