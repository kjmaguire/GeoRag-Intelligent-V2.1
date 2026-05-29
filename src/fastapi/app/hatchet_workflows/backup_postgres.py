"""§11.1 — nightly Postgres backup cron.

Schedule: ``0 2 * * *`` UTC (02:00 UTC, first in the staggered backup
window — see master_plan_section11_kickoff.md "Locked decisions").

What this workflow does
=======================

1. Insert a `backups.snapshot_runs` row with `status='running'`.
2. Run `pg_dump --format=custom --compress=6 --jobs=4` against the
   direct Postgres connection (bypassing pgbouncer — `pg_dump`
   needs `SET CLIENT_ENCODING`/`SET ROLE`-style commands that
   pgbouncer in transaction mode rejects).
3. Stream the dump bytes into the SeaweedFS bucket
   ``georag-backups`` under
   ``postgres/YYYY/MM/DD/HHMMSS-<run_id>.dump``.
4. Compute the sha256 hex digest + total bytes as the stream
   passes through.
5. Update the `backups.snapshot_runs` row with the bucket / key /
   sha / bytes / `status='completed'`.
6. Emit one `backup.postgres.snapshot.completed` audit row.

Failure handling
================

Any exception flips the row to `status='failed'` with the
exception's string representation in `failure_reason`. The audit
row is still emitted, with `payload.success=false`. Hatchet's
own retry policy decides whether to schedule another attempt.

Manual invocation
=================

``backup_postgres.run({})`` with no payload runs against the
defaults. ``backup_postgres.run({"bucket": "custom-bucket"})``
overrides the destination — useful for staging/dev runs that
shouldn't pollute the production cold-tier.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.backup_postgres")


class BackupPostgresInput(BaseModel):
    bucket: str = Field(
        default="georag-backups",
        description="SeaweedFS bucket to write the dump into. The bucket is "
                    "expected to exist; cron deploys must pre-create it.",
    )
    prefix: str = Field(
        default="postgres",
        description="Object-key prefix inside the bucket. Final key is "
                    "<prefix>/YYYY/MM/DD/HHMMSS-<run_id>.dump.",
    )


class BackupPostgresOutput(BaseModel):
    run_id: str
    status: str
    bucket: str
    object_key: str
    bytes: int
    sha256_hex: str
    started_at: datetime
    completed_at: datetime


backup_postgres = hatchet.workflow(
    name="backup_postgres",
    on_crons=["0 2 * * *"],
    input_validator=BackupPostgresInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _build_object_key(prefix: str, run_id: str, when: datetime) -> str:
    return (
        f"{prefix}/"
        f"{when.year:04d}/{when.month:02d}/{when.day:02d}/"
        f"{when.hour:02d}{when.minute:02d}{when.second:02d}-{run_id}.dump"
    )


async def _record_start(conn: asyncpg.Connection) -> str:
    """Insert the snapshot_runs row in `running` state; return its id."""
    row = await conn.fetchrow(
        """
        INSERT INTO backups.snapshot_runs (store, status)
        VALUES ('postgres', 'running')
        RETURNING run_id::text
        """,
    )
    return row["run_id"]


async def _record_completion(
    conn: asyncpg.Connection,
    run_id: str,
    *,
    bucket: str,
    object_key: str,
    sha256_hex: str,
    bytes_count: int,
) -> None:
    await conn.execute(
        """
        UPDATE backups.snapshot_runs
           SET status       = 'completed',
               completed_at = now(),
               bucket       = $2,
               object_key   = $3,
               sha256_hex   = $4,
               bytes        = $5,
               updated_at   = now()
         WHERE run_id = $1::uuid
        """,
        run_id, bucket, object_key, sha256_hex, bytes_count,
    )


async def _record_failure(
    conn: asyncpg.Connection, run_id: str, reason: str,
) -> None:
    await conn.execute(
        """
        UPDATE backups.snapshot_runs
           SET status         = 'failed',
               completed_at   = now(),
               failure_reason = $2,
               updated_at     = now()
         WHERE run_id = $1::uuid
        """,
        run_id, reason[:2000],  # cap to keep ledger compact
    )


async def _stream_pg_dump_to_seaweedfs(
    bucket: str, object_key: str, dsn: str,
) -> tuple[int, str]:
    """Spawn pg_dump, stream stdout into SeaweedFS S3, return (bytes, sha256_hex).

    Avoids materialising the whole dump in process memory unnecessarily
    by reading pg_dump's stdout in 4 MB chunks. The bytes are buffered
    until pg_dump exits cleanly, then uploaded via aioboto3 in a single
    PUT (the custom format isn't seekable so multipart streaming would
    need an explicit MultipartUpload state machine — out of scope for v1).
    """
    proc = await asyncio.create_subprocess_exec(
        "pg_dump",
        "--format=custom",
        "--compress=6",
        "--no-owner",
        "--no-privileges",
        dsn,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    hasher = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    assert proc.stdout is not None
    while True:
        chunk = await proc.stdout.read(4 * 1024 * 1024)
        if not chunk:
            break
        hasher.update(chunk)
        chunks.append(chunk)
        total += len(chunk)

    return_code = await proc.wait()
    if return_code != 0:
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
        raise RuntimeError(f"pg_dump exited with status {return_code}: {stderr[:500]}")

    await _put_s3(bucket, object_key, b"".join(chunks))
    return total, hasher.hexdigest()


async def _put_s3(bucket: str, key: str, body: bytes) -> None:
    """Upload to SeaweedFS via its S3 API using aioboto3.

    Configuration knobs (env vars, with safe dev defaults):
      - SEAWEEDFS_S3_ENDPOINT     — e.g. http://seaweedfs:8333
      - SEAWEEDFS_S3_ACCESS_KEY   — bucket policy IAM key
      - SEAWEEDFS_S3_SECRET_KEY   — paired secret
      - SEAWEEDFS_S3_REGION       — defaults to us-east-1 (SeaweedFS ignores)

    Bucket must exist (SeaweedFS S3 returns NoSuchBucket otherwise).
    Production deploys pre-create the bucket via the operator runbook.
    """
    import aioboto3

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "http://seaweedfs:8333")
    access_key = os.environ.get("SEAWEEDFS_S3_ACCESS_KEY", "georag")
    secret_key = os.environ.get("SEAWEEDFS_S3_SECRET_KEY", "georag")
    region = os.environ.get("SEAWEEDFS_S3_REGION", "us-east-1")

    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    ) as s3:
        await s3.put_object(Bucket=bucket, Key=key, Body=body)


@backup_postgres.task(execution_timeout="60m")
async def run_backup(input: BackupPostgresInput, ctx: Context) -> BackupPostgresOutput:
    started_at = datetime.now(tz=timezone.utc)
    dsn = _build_dsn()

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    run_id: str | None = None
    try:
        run_id = await _record_start(conn)
        object_key = _build_object_key(input.prefix, run_id, started_at)

        try:
            total, sha = await _stream_pg_dump_to_seaweedfs(
                input.bucket, object_key, dsn,
            )
        except Exception as exc:  # noqa: BLE001
            await _record_failure(conn, run_id, repr(exc))
            await emit_audit(
                conn,
                action_type="backup.postgres.snapshot.failed",
                workspace_id=None,
                actor_id=None,
                actor_kind="workflow",
                target_schema="backups",
                target_table="snapshot_runs",
                target_id=run_id,
                payload={
                    "store":  "postgres",
                    "reason": repr(exc)[:1000],
                    "bucket": input.bucket,
                    "key":    object_key,
                },
            )
            log.exception("backup_postgres failed run_id=%s", run_id)
            raise

        await _record_completion(
            conn, run_id,
            bucket=input.bucket,
            object_key=object_key,
            sha256_hex=sha,
            bytes_count=total,
        )

        completed_at = datetime.now(tz=timezone.utc)
        await emit_audit(
            conn,
            action_type="backup.postgres.snapshot.completed",
            workspace_id=None,
            actor_id=None,
            actor_kind="workflow",
            target_schema="backups",
            target_table="snapshot_runs",
            target_id=run_id,
            payload={
                "store":      "postgres",
                "bucket":     input.bucket,
                "key":        object_key,
                "bytes":      total,
                "sha256_hex": sha,
                "duration_s": (completed_at - started_at).total_seconds(),
            },
        )
        log.info(
            "backup_postgres OK run_id=%s bytes=%s sha=%s",
            run_id, total, sha[:12],
        )

        # Phase 5 admin surface push — drives Admin/BackupsDashboard +
        # Admin/WorkflowRuns. Best-effort.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "backup_postgres",
                "run_id": str(run_id),
                "store": "postgres",
                "status": "success",
                "bytes": total,
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="backups",
                affected_props=["snapshots", "snapshots_total"],
                payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "backup_postgres: admin surface broadcasts failed run_id=%s err=%s",
                run_id, exc,
            )

        return BackupPostgresOutput(
            run_id=run_id,
            status="completed",
            bucket=input.bucket,
            object_key=object_key,
            bytes=total,
            sha256_hex=sha,
            started_at=started_at,
            completed_at=completed_at,
        )
    finally:
        await conn.close()


__all__ = [
    "backup_postgres",
    "BackupPostgresInput",
    "BackupPostgresOutput",
]
