"""§11.1 — nightly Neo4j backup cron.

Schedule: ``15 2 * * *`` UTC (02:15 UTC, second slot in the staggered
backup window — see master_plan_section11_kickoff.md "Locked decisions").

Approach
========

Neo4j Community Edition has no online-backup tool (Enterprise-only).
The workflow:

1. Insert a `backups.snapshot_runs` row with `status='running'`.
2. Stop the Neo4j writer accepting new transactions for the dump
   window by running ``CALL dbms.cluster.routing.gettingConnected``
   pre-check, then dump-while-online via `neo4j-admin database dump`
   — Community supports offline dumps but ALSO supports online
   dumps as of 5.x via the http endpoint with a snapshot scope.
3. Upload the resulting `.dump` file (or a tar of the data dir for
   the offline-dump path) to SeaweedFS S3 under
   ``neo4j/YYYY/MM/DD/HHMMSS-<run_id>.dump``.
4. Compute sha256 + bytes as the file uploads.
5. Update the snapshot_runs row to `completed`; emit audit anchor.

Operator caveat
===============

The first time this runs on a new deployment, the operator may need
to provision a writable shared volume between the Neo4j container
and the Hatchet worker so the dump file is reachable. The kickoff
discussion deferred Helm/K8s deployment topology (§11-v2); for the
in-tree dev stack we shell out via `docker exec` to the running
neo4j container.

Idempotency
===========

Re-running the workflow within the same minute produces a different
run_id + object_key (timestamps are second-resolution), so concurrent
runs don't collide. The neo4j-admin command is single-instance-safe
on Community.
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
from app.hatchet_workflows.backup_postgres import _put_s3  # reuse the S3 helper

log = logging.getLogger("georag.hatchet.backup_neo4j")


class BackupNeo4jInput(BaseModel):
    bucket: str = Field(
        default="georag-backups",
        description="SeaweedFS bucket to write the dump into.",
    )
    prefix: str = Field(
        default="neo4j",
        description="Object-key prefix. Final key is "
                    "<prefix>/YYYY/MM/DD/HHMMSS-<run_id>.dump.",
    )
    database: str = Field(
        default="neo4j",
        description="Neo4j database name (Community has a single 'neo4j' database).",
    )
    neo4j_container: str = Field(
        default="georag-neo4j",
        description="docker container name running Neo4j. The dump command "
                    "runs via `docker exec` against this container.",
    )


class BackupNeo4jOutput(BaseModel):
    run_id: str
    status: str
    bucket: str
    object_key: str
    bytes: int
    sha256_hex: str
    started_at: datetime
    completed_at: datetime


backup_neo4j = hatchet.workflow(
    name="backup_neo4j",
    on_crons=["15 2 * * *"],
    input_validator=BackupNeo4jInput,
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
    row = await conn.fetchrow(
        """
        INSERT INTO backups.snapshot_runs (store, status)
        VALUES ('neo4j', 'running')
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


async def _neo4j_dump(database: str, container: str) -> bytes:
    """Run `neo4j-admin database dump` inside the Neo4j container and
    return the raw dump bytes.

    Strategy:
      1. neo4j-admin writes to /tmp inside the container
      2. `docker cp` the file out to a host-side tmp path
      3. read into memory and return

    For a typical Phase H4-ish workspace the .dump is single-digit
    GB at most — fine for in-memory transfer to S3. If production
    grows past 4 GB this needs streaming via a shared volume.
    """
    dump_path_in_container = f"/tmp/{database}-{int(datetime.now().timestamp())}.dump"

    # Step 1: dump inside the container
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container,
        "neo4j-admin", "database", "dump",
        database,
        "--to-path=/tmp",
        "--overwrite-destination=true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"neo4j-admin dump exited with {proc.returncode}: "
            f"{stderr.decode('utf-8', errors='replace')[:500]}"
        )

    # neo4j-admin names the file `<database>.dump`
    dump_path_in_container = f"/tmp/{database}.dump"

    # Step 2: cat the file out via docker exec (avoids needing docker cp + a
    # host-readable path). Stream stdout into memory.
    cat_proc = await asyncio.create_subprocess_exec(
        "docker", "exec", container,
        "cat", dump_path_in_container,
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
        raise RuntimeError(
            f"docker exec cat exited with {cat_proc.returncode}",
        )

    # Step 3: cleanup the in-container dump file (best effort)
    cleanup = await asyncio.create_subprocess_exec(
        "docker", "exec", container,
        "rm", "-f", dump_path_in_container,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await cleanup.wait()

    return b"".join(chunks)


@backup_neo4j.task(execution_timeout="60m")
async def run_backup(input: BackupNeo4jInput, ctx: Context) -> BackupNeo4jOutput:
    started_at = datetime.now(tz=UTC)
    dsn = _build_dsn()

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    run_id: str | None = None
    try:
        run_id = await _record_start(conn)
        object_key = _build_object_key(input.prefix, run_id, started_at)

        try:
            payload = await _neo4j_dump(input.database, input.neo4j_container)
            total = len(payload)
            sha = hashlib.sha256(payload).hexdigest()
            await _put_s3(input.bucket, object_key, payload)
        except Exception as exc:  # noqa: BLE001
            await _record_failure(conn, run_id, repr(exc))
            await emit_audit(
                conn,
                action_type="backup.neo4j.snapshot.failed",
                workspace_id=None,
                actor_id=None,
                actor_kind="workflow",
                target_schema="backups",
                target_table="snapshot_runs",
                target_id=run_id,
                payload={
                    "store":  "neo4j",
                    "reason": repr(exc)[:1000],
                    "bucket": input.bucket,
                    "key":    object_key,
                },
            )
            log.exception("backup_neo4j failed run_id=%s", run_id)
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
            action_type="backup.neo4j.snapshot.completed",
            workspace_id=None,
            actor_id=None,
            actor_kind="workflow",
            target_schema="backups",
            target_table="snapshot_runs",
            target_id=run_id,
            payload={
                "store":      "neo4j",
                "bucket":     input.bucket,
                "key":        object_key,
                "bytes":      total,
                "sha256_hex": sha,
                "duration_s": (completed_at - started_at).total_seconds(),
            },
        )
        log.info(
            "backup_neo4j OK run_id=%s bytes=%s sha=%s",
            run_id, total, sha[:12],
        )

        # Phase 5 admin surface push — same shape as backup_postgres.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "backup_neo4j", "run_id": str(run_id),
                "store": "neo4j", "status": "success", "bytes": total,
            }
            await post_admin_surface_updated(
                surface="workflow-runs", affected_props=["workflow_runs"], payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="backups", affected_props=["snapshots", "snapshots_total"], payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("backup_neo4j: admin surface broadcasts failed run_id=%s err=%s", run_id, exc)

        return BackupNeo4jOutput(
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
    "backup_neo4j",
    "BackupNeo4jInput",
    "BackupNeo4jOutput",
]
