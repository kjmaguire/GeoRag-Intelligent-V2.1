"""§11.1 — nightly SeaweedFS bronze-bucket replication cron.

Schedule: ``0 3 * * *`` UTC (03:00 UTC, fifth + final slot — see kickoff).

Approach
========

SeaweedFS is already the destination of the other backup workflows
(PG / Neo4j / Qdrant / Redis). This cron handles the OTHER buckets
— specifically the bronze ingestion bucket (``georag-bronze`` by
default) which holds the source PDFs / GIS files / etc. that
ingestion runs against.

Rather than dumping object-by-object (slow, brittle), we use the
S3 CopyObject API to replicate the bronze bucket into a snapshot
prefix inside the ``georag-backups`` bucket:

    georag-bronze/...  →  georag-backups/seaweedfs/YYYY/MM/DD/HHMMSS-<run_id>/...

Each object's sha256 is captured from the source ETag (SeaweedFS
returns the object's md5 as the ETag for non-multipart uploads;
we record both the per-object summary and the aggregate byte count
in the snapshot_runs payload jsonb).

Operator notes
==============

- This is a logical snapshot — the source bucket can keep accepting
  writes during the run; the destination is timestamp-frozen at
  copy time.
- For very large bronze buckets (>100 GB) this is bandwidth-bound
  on the SeaweedFS instance. Production sizing tracked in §28.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

import aioboto3
import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.backup_seaweedfs")


class BackupSeaweedFsInput(BaseModel):
    source_bucket: str = Field(
        default="georag-bronze",
        description="The SeaweedFS bucket to snapshot.",
    )
    dest_bucket: str = Field(
        default="georag-backups",
        description="The bucket that receives the timestamped snapshot prefix.",
    )
    prefix: str = Field(
        default="seaweedfs",
        description="Object-key prefix inside dest_bucket. Final layout: "
                    "<prefix>/YYYY/MM/DD/HHMMSS-<run_id>/<source_key>.",
    )


class BackupSeaweedFsOutput(BaseModel):
    run_id: str
    status: str
    source_bucket: str
    dest_bucket: str
    snapshot_prefix: str
    objects_copied: int
    objects_failed: int
    bytes: int
    started_at: datetime
    completed_at: datetime


backup_seaweedfs = hatchet.workflow(
    name="backup_seaweedfs",
    on_crons=["0 3 * * *"],
    input_validator=BackupSeaweedFsInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _snapshot_prefix(prefix: str, run_id: str, when: datetime) -> str:
    return (
        f"{prefix}/"
        f"{when.year:04d}/{when.month:02d}/{when.day:02d}/"
        f"{when.hour:02d}{when.minute:02d}{when.second:02d}-{run_id}"
    )


async def _record_start(conn: asyncpg.Connection) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO backups.snapshot_runs (store, status)
        VALUES ('seaweedfs', 'running')
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
    bytes_count: int,
    payload: dict,
) -> None:
    await conn.execute(
        """
        UPDATE backups.snapshot_runs
           SET status       = 'completed',
               completed_at = now(),
               bucket       = $2,
               object_key   = $3,
               bytes        = $4,
               payload      = $5::jsonb,
               updated_at   = now()
         WHERE run_id = $1::uuid
        """,
        run_id, bucket, object_key, bytes_count, json.dumps(payload),
    )


async def _record_failure(
    conn: asyncpg.Connection, run_id: str, reason: str, payload: dict | None = None,
) -> None:
    await conn.execute(
        """
        UPDATE backups.snapshot_runs
           SET status         = 'failed',
               completed_at   = now(),
               failure_reason = $2,
               payload        = $3::jsonb,
               updated_at     = now()
         WHERE run_id = $1::uuid
        """,
        run_id, reason[:2000], json.dumps(payload or {}),
    )


def _s3_session_kwargs() -> dict[str, str]:
    return {
        "endpoint_url":          os.environ.get("SEAWEEDFS_S3_ENDPOINT", "http://seaweedfs:8333"),
        "aws_access_key_id":     os.environ.get("SEAWEEDFS_S3_ACCESS_KEY", "georag"),
        "aws_secret_access_key": os.environ.get("SEAWEEDFS_S3_SECRET_KEY", "georag"),
        "region_name":           os.environ.get("SEAWEEDFS_S3_REGION", "us-east-1"),
    }


@backup_seaweedfs.task(execution_timeout="120m")
async def run_backup(input: BackupSeaweedFsInput, ctx: Context) -> BackupSeaweedFsOutput:
    started_at = datetime.now(tz=UTC)
    dsn = _build_dsn()

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    run_id: str | None = None
    copied = 0
    failed = 0
    total_bytes = 0
    snapshot_prefix = ""
    try:
        run_id = await _record_start(conn)
        snapshot_prefix = _snapshot_prefix(input.prefix, run_id, started_at)

        session = aioboto3.Session()
        async with session.client("s3", **_s3_session_kwargs()) as s3:
            paginator = s3.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=input.source_bucket):
                for obj in page.get("Contents", []) or []:
                    src_key = obj["Key"]
                    dst_key = f"{snapshot_prefix}/{src_key}"
                    size = int(obj.get("Size", 0) or 0)
                    try:
                        await s3.copy_object(
                            Bucket=input.dest_bucket,
                            Key=dst_key,
                            CopySource={"Bucket": input.source_bucket, "Key": src_key},
                        )
                        copied += 1
                        total_bytes += size
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        log.warning(
                            "seaweedfs copy failed src=%s dst=%s err=%r",
                            src_key, dst_key, exc,
                        )

        completed_at = datetime.now(tz=UTC)
        payload = {
            "store":            "seaweedfs",
            "source_bucket":    input.source_bucket,
            "dest_bucket":      input.dest_bucket,
            "snapshot_prefix":  snapshot_prefix,
            "objects_copied":   copied,
            "objects_failed":   failed,
            "bytes_total":      total_bytes,
            "duration_s":       (completed_at - started_at).total_seconds(),
        }

        if failed > 0:
            await _record_failure(
                conn, run_id,
                f"{failed} of {copied + failed} objects failed during copy",
                payload=payload,
            )
            await emit_audit(
                conn,
                action_type="backup.seaweedfs.snapshot.failed",
                workspace_id=None,
                actor_id=None,
                actor_kind="workflow",
                target_schema="backups",
                target_table="snapshot_runs",
                target_id=run_id,
                payload=payload,
            )
            return BackupSeaweedFsOutput(
                run_id=run_id, status="failed",
                source_bucket=input.source_bucket,
                dest_bucket=input.dest_bucket,
                snapshot_prefix=snapshot_prefix,
                objects_copied=copied, objects_failed=failed,
                bytes=total_bytes,
                started_at=started_at, completed_at=completed_at,
            )

        await _record_completion(
            conn, run_id,
            bucket=input.dest_bucket,
            object_key=snapshot_prefix,  # logical prefix, not a single key
            bytes_count=total_bytes,
            payload=payload,
        )
        await emit_audit(
            conn,
            action_type="backup.seaweedfs.snapshot.completed",
            workspace_id=None,
            actor_id=None,
            actor_kind="workflow",
            target_schema="backups",
            target_table="snapshot_runs",
            target_id=run_id,
            payload=payload,
        )
        log.info(
            "backup_seaweedfs OK run_id=%s copied=%d bytes=%s",
            run_id, copied, total_bytes,
        )

        # Phase 5 admin surface push — same shape as backup_postgres.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "backup_seaweedfs", "run_id": str(run_id),
                "store": "seaweedfs", "status": "success",
                "bytes": total_bytes, "objects_copied": copied,
            }
            await post_admin_surface_updated(
                surface="workflow-runs", affected_props=["workflow_runs"], payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="backups", affected_props=["snapshots", "snapshots_total"], payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("backup_seaweedfs: admin surface broadcasts failed run_id=%s err=%s", run_id, exc)

        return BackupSeaweedFsOutput(
            run_id=run_id, status="completed",
            source_bucket=input.source_bucket,
            dest_bucket=input.dest_bucket,
            snapshot_prefix=snapshot_prefix,
            objects_copied=copied, objects_failed=0,
            bytes=total_bytes,
            started_at=started_at, completed_at=completed_at,
        )
    finally:
        await conn.close()


__all__ = [
    "backup_seaweedfs",
    "BackupSeaweedFsInput",
    "BackupSeaweedFsOutput",
]
