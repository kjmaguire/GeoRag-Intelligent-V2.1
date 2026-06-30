"""§11.1 — nightly Redis backup cron.

Schedule: ``45 2 * * *`` UTC (02:45 UTC, fourth slot — see kickoff).

Approach
========

Trigger a synchronous BGSAVE inside the Redis container, wait for
the rdb file to land in `/data/dump.rdb`, then `docker exec cat`
it out and ship to SeaweedFS under
``redis/YYYY/MM/DD/HHMMSS-<run_id>.rdb``.

Redis is largely cache + outbox state in this app — losing some of
it on restore is recoverable (caches refill, outbox replays). But
the workspace-resolution + agentic-retrieval workspace-flags
cache are mildly precious; nightly RDB is sufficient.

Operator notes
==============

- BGSAVE is non-blocking but a forked process; needs ~the resident
  set size of free RAM transiently. For the dev stack this is
  trivially small (<100 MB). Production sizing tracked in §28.
- BGSAVE has a built-in cooldown; calling twice within the same
  second returns "Background save already in progress". The
  workflow waits 0.5s then retries up to 3 times before failing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import UTC, datetime

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet
from app.hatchet_workflows.backup_postgres import _put_s3

log = logging.getLogger("georag.hatchet.backup_redis")


class BackupRedisInput(BaseModel):
    bucket: str = Field(default="georag-backups")
    prefix: str = Field(default="redis")
    redis_container: str = Field(default="georag-redis")
    rdb_path_in_container: str = Field(default="/data/dump.rdb")


class BackupRedisOutput(BaseModel):
    run_id: str
    status: str
    bucket: str
    object_key: str
    bytes: int
    sha256_hex: str
    started_at: datetime
    completed_at: datetime


backup_redis = hatchet.workflow(
    name="backup_redis",
    on_crons=["45 2 * * *"],
    input_validator=BackupRedisInput,
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
        f"{when.hour:02d}{when.minute:02d}{when.second:02d}-{run_id}.rdb"
    )


async def _record_start(conn: asyncpg.Connection) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO backups.snapshot_runs (store, status)
        VALUES ('redis', 'running')
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
        run_id, reason[:2000],
    )


async def _trigger_bgsave_and_fetch(
    container: str, rdb_path: str,
) -> bytes:
    """BGSAVE, poll LASTSAVE until it advances, then cat the RDB out."""
    # Read pre-save LASTSAVE
    pre_proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container,
        "redis-cli", "LASTSAVE",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    pre_out, _ = await pre_proc.communicate()
    pre_lastsave = int(pre_out.decode().strip() or "0")

    # Trigger BGSAVE, retry up to 3x if "save already in progress"
    last_err = ""
    for attempt in range(3):
        bg_proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container,
            "redis-cli", "BGSAVE",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        bg_out, bg_err = await bg_proc.communicate()
        out_text = bg_out.decode().strip()
        if "Background save started" in out_text or out_text == "Background saving started":
            break
        last_err = out_text or bg_err.decode().strip()
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError(f"BGSAVE failed after 3 attempts: {last_err}")

    # Poll until LASTSAVE advances (BGSAVE complete)
    for _ in range(120):  # up to 60 s
        await asyncio.sleep(0.5)
        poll_proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container,
            "redis-cli", "LASTSAVE",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        poll_out, _ = await poll_proc.communicate()
        if int(poll_out.decode().strip() or "0") > pre_lastsave:
            break
    else:
        raise RuntimeError("BGSAVE did not complete within 60s")

    # Cat the RDB out
    cat_proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container,
        "cat", rdb_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    chunks: list[bytes] = []
    assert cat_proc.stdout is not None
    while True:
        chunk = await cat_proc.stdout.read(4 * 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    await cat_proc.wait()
    if cat_proc.returncode != 0:
        raise RuntimeError(f"cat {rdb_path} exited with {cat_proc.returncode}")

    return b"".join(chunks)


@backup_redis.task(execution_timeout="15m")
async def run_backup(input: BackupRedisInput, ctx: Context) -> BackupRedisOutput:
    started_at = datetime.now(tz=UTC)
    dsn = _build_dsn()

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    run_id: str | None = None
    try:
        run_id = await _record_start(conn)
        object_key = _build_object_key(input.prefix, run_id, started_at)

        try:
            payload = await _trigger_bgsave_and_fetch(
                input.redis_container, input.rdb_path_in_container,
            )
            total = len(payload)
            sha = hashlib.sha256(payload).hexdigest()
            await _put_s3(input.bucket, object_key, payload)
        except Exception as exc:  # noqa: BLE001
            await _record_failure(conn, run_id, repr(exc))
            await emit_audit(
                conn,
                action_type="backup.redis.snapshot.failed",
                workspace_id=None,
                actor_id=None,
                actor_kind="workflow",
                target_schema="backups",
                target_table="snapshot_runs",
                target_id=run_id,
                payload={"store": "redis", "reason": repr(exc)[:1000]},
            )
            log.exception("backup_redis failed run_id=%s", run_id)
            raise

        await _record_completion(
            conn, run_id,
            bucket=input.bucket,
            object_key=object_key,
            sha256_hex=sha,
            bytes_count=total,
        )

        completed_at = datetime.now(tz=UTC)
        await emit_audit(
            conn,
            action_type="backup.redis.snapshot.completed",
            workspace_id=None,
            actor_id=None,
            actor_kind="workflow",
            target_schema="backups",
            target_table="snapshot_runs",
            target_id=run_id,
            payload={
                "store":      "redis",
                "bucket":     input.bucket,
                "key":        object_key,
                "bytes":      total,
                "sha256_hex": sha,
                "duration_s": (completed_at - started_at).total_seconds(),
            },
        )
        log.info(
            "backup_redis OK run_id=%s bytes=%s sha=%s",
            run_id, total, sha[:12],
        )

        # Phase 5 admin surface push — same shape as backup_postgres.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "backup_redis", "run_id": str(run_id),
                "store": "redis", "status": "success", "bytes": total,
            }
            await post_admin_surface_updated(
                surface="workflow-runs", affected_props=["workflow_runs"], payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="backups", affected_props=["snapshots", "snapshots_total"], payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("backup_redis: admin surface broadcasts failed run_id=%s err=%s", run_id, exc)

        return BackupRedisOutput(
            run_id=run_id, status="completed",
            bucket=input.bucket, object_key=object_key,
            bytes=total, sha256_hex=sha,
            started_at=started_at, completed_at=completed_at,
        )
    finally:
        await conn.close()


__all__ = [
    "backup_redis",
    "BackupRedisInput",
    "BackupRedisOutput",
]
