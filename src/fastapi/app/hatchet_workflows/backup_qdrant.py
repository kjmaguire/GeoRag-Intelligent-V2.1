"""§11.1 — nightly Qdrant backup cron.

Schedule: ``30 2 * * *`` UTC (02:30 UTC, third slot — see kickoff).

Approach
========

Qdrant exposes a snapshot API per collection. The workflow walks
every collection in the cluster, calls `create_snapshot`, downloads
the resulting `.snapshot` blob from `/collections/{name}/snapshots/{file}`,
and uploads it to SeaweedFS under
``qdrant/YYYY/MM/DD/HHMMSS-<run_id>/<collection>.snapshot``.

A single run_id covers all collections; the audit row payload lists
every collection that was successfully snapshotted (so partial
failures are visible). If a collection fails mid-run we continue
with the rest but flip status='failed' at the end.

Operator notes
==============

- Qdrant snapshots include both vectors AND payloads, so a single
  snapshot is a complete restore source.
- For very large collections (>10 GB), the in-memory transfer here
  is a bottleneck; deferred to §11-v2 streaming refactor.
- The snapshot endpoint is unauthenticated by default in the dev
  stack; production deploys with QDRANT_API_KEY set will need the
  client to forward it (already handled by qdrant-client lib).
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone

import asyncpg
import httpx
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet
from app.hatchet_workflows.backup_postgres import _put_s3

log = logging.getLogger("georag.hatchet.backup_qdrant")


class BackupQdrantInput(BaseModel):
    bucket: str = Field(default="georag-backups")
    prefix: str = Field(default="qdrant")


class BackupQdrantOutput(BaseModel):
    run_id: str
    status: str
    bucket: str
    collections_snapshotted: list[str]
    collections_failed: list[str]
    bytes: int
    started_at: datetime
    completed_at: datetime


backup_qdrant = hatchet.workflow(
    name="backup_qdrant",
    on_crons=["30 2 * * *"],
    input_validator=BackupQdrantInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _build_object_key(prefix: str, run_id: str, when: datetime, collection: str) -> str:
    return (
        f"{prefix}/"
        f"{when.year:04d}/{when.month:02d}/{when.day:02d}/"
        f"{when.hour:02d}{when.minute:02d}{when.second:02d}-{run_id}/"
        f"{collection}.snapshot"
    )


def _qdrant_base_url() -> str:
    host = os.environ.get("QDRANT_HOST", "qdrant")
    port = os.environ.get("QDRANT_PORT", "6333")
    return f"http://{host}:{port}"


async def _record_start(conn: asyncpg.Connection) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO backups.snapshot_runs (store, status)
        VALUES ('qdrant', 'running')
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
    payload: dict,
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
               payload      = $6::jsonb,
               updated_at   = now()
         WHERE run_id = $1::uuid
        """,
        run_id, bucket, object_key, sha256_hex, bytes_count,
        __import__("json").dumps(payload),
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
        run_id, reason[:2000],
        __import__("json").dumps(payload or {}),
    )


async def _snapshot_one_collection(
    client: httpx.AsyncClient,
    base_url: str,
    collection: str,
    bucket: str,
    object_key: str,
) -> tuple[int, str]:
    """Trigger + download + upload one collection's snapshot.
    Returns (bytes, sha256_hex)."""
    create = await client.post(
        f"{base_url}/collections/{collection}/snapshots",
    )
    if create.status_code >= 400:
        raise RuntimeError(
            f"snapshot create {collection} HTTP {create.status_code}: {create.text[:200]}",
        )
    snapshot_name = create.json()["result"]["name"]

    download = await client.get(
        f"{base_url}/collections/{collection}/snapshots/{snapshot_name}",
    )
    if download.status_code >= 400:
        raise RuntimeError(
            f"snapshot download {collection} HTTP {download.status_code}",
        )
    body = download.content
    sha = hashlib.sha256(body).hexdigest()
    await _put_s3(bucket, object_key, body)
    return len(body), sha


@backup_qdrant.task(execution_timeout="60m")
async def run_backup(input: BackupQdrantInput, ctx: Context) -> BackupQdrantOutput:
    started_at = datetime.now(tz=timezone.utc)
    base_url = _qdrant_base_url()
    dsn = _build_dsn()

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    run_id: str | None = None
    snapshotted: list[str] = []
    failed: list[str] = []
    total_bytes = 0
    last_object_key = ""
    last_sha = ""
    try:
        run_id = await _record_start(conn)

        async with httpx.AsyncClient(timeout=300.0) as client:
            # Discover collections
            resp = await client.get(f"{base_url}/collections")
            resp.raise_for_status()
            collections = [c["name"] for c in resp.json()["result"]["collections"]]

            for collection in collections:
                object_key = _build_object_key(
                    input.prefix, run_id, started_at, collection,
                )
                try:
                    size, sha = await _snapshot_one_collection(
                        client, base_url, collection, input.bucket, object_key,
                    )
                    snapshotted.append(collection)
                    total_bytes += size
                    last_object_key = object_key
                    last_sha = sha
                except Exception as exc:  # noqa: BLE001
                    failed.append(collection)
                    log.warning(
                        "backup_qdrant collection=%s failed err=%r",
                        collection, exc,
                    )

        completed_at = datetime.now(tz=timezone.utc)
        payload = {
            "store":                    "qdrant",
            "bucket":                   input.bucket,
            "collections_snapshotted":  snapshotted,
            "collections_failed":       failed,
            "bytes_total":              total_bytes,
            "duration_s":               (completed_at - started_at).total_seconds(),
        }

        if failed:
            await _record_failure(
                conn, run_id,
                f"{len(failed)} of {len(snapshotted) + len(failed)} collections failed: {failed}",
                payload=payload,
            )
            await emit_audit(
                conn,
                action_type="backup.qdrant.snapshot.failed",
                workspace_id=None,
                actor_id=None,
                actor_kind="workflow",
                target_schema="backups",
                target_table="snapshot_runs",
                target_id=run_id,
                payload=payload,
            )
            return BackupQdrantOutput(
                run_id=run_id, status="failed",
                bucket=input.bucket,
                collections_snapshotted=snapshotted,
                collections_failed=failed,
                bytes=total_bytes,
                started_at=started_at, completed_at=completed_at,
            )

        # All collections snapshotted successfully — the snapshot_runs row
        # carries the LAST collection's key/sha for the registry (the
        # payload jsonb holds the full collection list).
        await _record_completion(
            conn, run_id,
            bucket=input.bucket,
            object_key=last_object_key,
            sha256_hex=last_sha,
            bytes_count=total_bytes,
            payload=payload,
        )
        await emit_audit(
            conn,
            action_type="backup.qdrant.snapshot.completed",
            workspace_id=None,
            actor_id=None,
            actor_kind="workflow",
            target_schema="backups",
            target_table="snapshot_runs",
            target_id=run_id,
            payload=payload,
        )
        log.info(
            "backup_qdrant OK run_id=%s collections=%d bytes=%s",
            run_id, len(snapshotted), total_bytes,
        )

        # Phase 5 admin surface push — same shape as backup_postgres.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "backup_qdrant", "run_id": str(run_id),
                "store": "qdrant", "status": "success", "bytes": total_bytes,
                "collections_snapshotted": len(snapshotted),
            }
            await post_admin_surface_updated(
                surface="workflow-runs", affected_props=["workflow_runs"], payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="backups", affected_props=["snapshots", "snapshots_total"], payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("backup_qdrant: admin surface broadcasts failed run_id=%s err=%s", run_id, exc)

        return BackupQdrantOutput(
            run_id=run_id, status="completed",
            bucket=input.bucket,
            collections_snapshotted=snapshotted,
            collections_failed=[],
            bytes=total_bytes,
            started_at=started_at, completed_at=completed_at,
        )
    finally:
        await conn.close()


__all__ = [
    "backup_qdrant",
    "BackupQdrantInput",
    "BackupQdrantOutput",
]
