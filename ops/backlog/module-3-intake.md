# Module 3 (Ingestion Pipeline) — pre-approved intake items

Items flagged during Modules 1–2 that Kyle has pre-approved for Module 3 execution.
Raised outside Module 3's own scope so they stay out of the one-module-at-a-time lane;
landed here as the canonical handoff so Module 3 Phase A picks them up first.

## Dagster MinIOResource → boto3 refactor

- **Raised:** 2026-04-19 Module 2 Phase B tuning (SeaweedFS vendor-purity sweep)
- **Source:** `src/dagster/georag_dagster/resources.py:20-21` imports `from minio import Minio` and calls vendor-specific methods (`fput_object`, `bucket_exists`, `make_bucket`) on line ~103
- **Rationale:** addendum §02a vendor-purity rule — application code must use boto3 with `endpoint_url`, not SeaweedFS-native or MinIO-admin SDKs
- **Approach:** drop-in boto3 replacement. Same method names exist on `boto3.client('s3').Bucket()` or can be adapted. `put_object` replaces `fput_object`. Bucket existence via `head_bucket`. Bucket creation via `create_bucket`. Endpoint and auth from env (`AWS_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) — these are already in `.env` per Module 1 Phase B.
- **Approval:** Kyle pre-approved 2026-04-19 in context of Module 2 Phase B decision pass. Execute at the start of Module 3 Phase B.
- **Owner:** data-engineer agent during Module 3
- **STATUS: RESOLVED 2026-04-20** — Chunk 3a. `MinIOResource` replaced with `S3Resource` (boto3). All call sites updated. minio-py removed from pyproject.toml. boto3 1.42.92 confirmed. Smoke test: bucket_exists('bronze') = True. See ops/audit/2026-04-20-ingestion-audit.md.

---

## Phase B cleanup — items deferred from Phase B1+B2 (2026-04-20)

### Stale georag-exports prefix in bronze bucket — RESOLVED 2026-04-21

- **Finding:** Module 2 Phase C renamed `georag-bronze` → `bronze` and `georag-exports` → `exports`.
  The sensor docstring drift was confirmed and fixed in Phase B1+B2. Module 3 Chunk 1 deleted the
  3 stray artifacts under `bronze/georag-exports/` prefix (5.8 KiB total).
- **Resolution:** Stray artifacts removed in Module 3 Phase B Chunk 1 (2026-04-20). A targeted
  grep of active code for `georag-exports` references during Module 3 Chunk 3a (MinIOResource → boto3)
  found none remaining. Closed inline during cross-module cleanup sweep 2026-04-21.
- **Raised:** 2026-04-20 — **Closed 2026-04-21**
