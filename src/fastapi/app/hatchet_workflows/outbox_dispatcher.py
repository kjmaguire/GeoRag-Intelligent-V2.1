"""Phase 0 step 4.3 — Hatchet workflow that drains the outbox to secondary stores.

Polls ``outbox.pending_propagations`` for ``status='pending'`` rows, dispatches
each to its target store (``qdrant`` / ``neo4j`` / ``seaweedfs``), and writes
one row per attempt to ``outbox.propagation_attempts``. Per-target concurrency
is bounded by an ``asyncio.Semaphore`` (Qdrant=10, Neo4j=4, SeaweedFS=8 by
default; overridable via ``target_store_concurrency_hint`` on the row).

After ``dead_letter_after_attempts`` consecutive transient failures the row is
marked ``dead_lettered`` and a ``silver.store_reconciliation_findings`` row is
written so the Store Reconciliation Agent (Phase 0 R0) can investigate. Every
attempt also calls ``emit_audit()`` so finds-back through the audit ledger is
possible.

Cadence: cron ``* * * * *`` triggers a fresh invocation each minute; each
invocation runs an internal poll loop for ``max_runtime_seconds`` (default 55)
so there is always a fresh poller running. Phase 11 will replace this with a
true long-running loop once production cadence requirements are firmed up.

Payload shapes per target store:

* ``qdrant``  — ``{"vector": [...], "metadata": {...}, "collection": "..."}``
                ``id`` taken from ``idempotency_key``
                operation ``upsert`` or ``delete``
* ``neo4j``   — ``{"cypher": "...", "params": {...}}``
* ``seaweedfs`` — ``{"bucket": "...", "key": "...", "body": "<bytes-or-str>"}``
* ``external_webhook`` — ``{"webhook_url": "<override-url>", ...arbitrary body}``
                     target_collection routes to per-channel webhook URL via
                     ``ACTIVEPIECES_WEBHOOK_URL_<COLLECTION>`` env var; request
                     is signed with HMAC-SHA256 over canonical JSON.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from typing import Any
from uuid import UUID

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet


class OutboxDispatcherInput(BaseModel):
    max_runtime_seconds: int = Field(default=55, ge=1, le=300)
    poll_interval_seconds: float = Field(default=1.0, ge=0.05, le=10.0)
    batch_size: int = Field(default=25, ge=1, le=500)
    dead_letter_after_attempts: int = Field(default=3, ge=1, le=20)


class OutboxDispatcherOutput(BaseModel):
    rows_processed: int
    rows_succeeded: int
    rows_transient_failed: int
    rows_dead_lettered: int
    elapsed_seconds: float


outbox_dispatcher = hatchet.workflow(
    name="outbox_dispatcher",
    on_crons=["* * * * *"],
    input_validator=OutboxDispatcherInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


# ---------------------------------------------------------------------------
# Per-target concurrency limits.
# ---------------------------------------------------------------------------
_DEFAULT_LIMITS = {"qdrant": 10, "neo4j": 4, "seaweedfs": 8}
_TARGET_SEMAPHORES: dict[str, asyncio.Semaphore] = {}


def _semaphore_for(target_store: str, hint: int | None) -> asyncio.Semaphore:
    if target_store not in _TARGET_SEMAPHORES:
        limit = _DEFAULT_LIMITS.get(target_store, hint or 4)
        _TARGET_SEMAPHORES[target_store] = asyncio.Semaphore(limit)
    return _TARGET_SEMAPHORES[target_store]


# ---------------------------------------------------------------------------
# Per-target dispatchers.
# Each returns (status, error_message) where status ∈ {success,
# transient_failure, permanent_failure}.
# ---------------------------------------------------------------------------
def _payload(row: asyncpg.Record) -> dict[str, Any]:
    raw = row["payload"]
    if isinstance(raw, str):
        return json.loads(raw)
    return dict(raw or {})


async def _dispatch_qdrant(row: asyncpg.Record) -> tuple[str, str | None]:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as qmodels

    payload = _payload(row)
    collection = (
        row["target_collection"]
        or payload.get("collection")
        or os.environ.get("QDRANT_DEFAULT_COLLECTION", "georag_default")
    )

    client = AsyncQdrantClient(
        host=os.environ.get("QDRANT_HOST", "qdrant"),
        port=int(os.environ.get("QDRANT_PORT", "6333")),
        api_key=os.environ.get("QDRANT_API_KEY") or None,
        prefer_grpc=False,
    )
    try:
        if row["operation"] == "upsert":
            vector = payload.get("vector")
            if vector is None:
                return "permanent_failure", "missing 'vector' in payload"
            await client.upsert(
                collection_name=collection,
                points=[
                    qmodels.PointStruct(
                        id=row["idempotency_key"],
                        vector=vector,
                        payload=payload.get("metadata") or {},
                    )
                ],
                wait=True,
            )
        elif row["operation"] == "delete":
            await client.delete(
                collection_name=collection,
                points_selector=qmodels.PointIdsList(points=[row["idempotency_key"]]),
                wait=True,
            )
        else:
            return "permanent_failure", f"unsupported operation {row['operation']!r}"
        return "success", None
    except Exception as exc:
        return "transient_failure", f"{type(exc).__name__}: {exc}"
    finally:
        await client.close()


async def _dispatch_neo4j(row: asyncpg.Record) -> tuple[str, str | None]:
    from neo4j import AsyncGraphDatabase

    payload = _payload(row)
    cypher = payload.get("cypher")
    if cypher is None:
        return "permanent_failure", "missing 'cypher' in payload"
    params = payload.get("params") or {}

    uri = (
        f"bolt://{os.environ.get('NEO4J_HOST', 'neo4j')}:"
        f"{os.environ.get('NEO4J_PORT', '7687')}"
    )
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")

    driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    try:
        async with driver.session() as session:
            await session.run(cypher, **params)
        return "success", None
    except Exception as exc:
        return "transient_failure", f"{type(exc).__name__}: {exc}"
    finally:
        await driver.close()


async def _dispatch_seaweedfs(row: asyncpg.Record) -> tuple[str, str | None]:
    import boto3

    payload = _payload(row)
    bucket = (
        payload.get("bucket")
        or row["target_collection"]
        or os.environ.get("SEAWEEDFS_DEFAULT_BUCKET", "georag-default")
    )
    key = payload.get("key") or row["idempotency_key"]
    body = payload.get("body")
    if body is None:
        return "permanent_failure", "missing 'body' in payload"
    if isinstance(body, str):
        body = body.encode("utf-8")

    endpoint = os.environ.get("SEAWEEDFS_S3_ENDPOINT", "http://minio:8333")
    access_key = os.environ.get(
        "SEAWEEDFS_ACCESS_KEY", os.environ.get("MINIO_ROOT_USER", "")
    )
    secret_key = os.environ.get(
        "SEAWEEDFS_SECRET_KEY", os.environ.get("MINIO_ROOT_PASSWORD", "")
    )

    def _put() -> None:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
        )
        if row["operation"] == "delete":
            client.delete_object(Bucket=bucket, Key=key)
        else:
            client.put_object(Bucket=bucket, Key=key, Body=body)

    try:
        await asyncio.to_thread(_put)
        return "success", None
    except Exception as exc:
        return "transient_failure", f"{type(exc).__name__}: {exc}"


async def _dispatch_external_webhook(row: asyncpg.Record) -> tuple[str, str | None]:
    """POST the outbox payload to an external integration webhook (Kestra
    flow, downstream SaaS endpoint, etc.).

    Phase H4 §7 wiring. The webhook URL is resolved in this order:
      1. ``payload.webhook_url`` (per-event override)
      2. ``EXTERNAL_WEBHOOK_URL_<target_collection>`` env var
         (per-channel routing; e.g. ``EXTERNAL_WEBHOOK_URL_NOTIFICATIONS``)
      3. ``EXTERNAL_WEBHOOK_URL_DEFAULT`` env var

    The dispatcher signs the request body with HMAC-SHA256 over the
    canonical JSON using ``EXTERNAL_WEBHOOK_HMAC_SECRET``; the receiving
    integration must verify the signature header
    ``X-GeoRAG-Signature: sha256=<hex>``. Idempotency_key flows through
    in the header ``X-GeoRAG-Idempotency-Key`` so the receiver can
    short-circuit duplicate deliveries.

    Returns the (status, error_message) tuple required by the dispatcher
    contract. ``transient_failure`` triggers retry; ``permanent_failure``
    routes to dead-letter.

    Renamed from ``_dispatch_activepieces`` 2026-05-17 after the
    Activepieces sunset; the same HMAC-signed POST pattern serves Kestra
    and any other downstream webhook target.
    """
    import httpx

    payload = _payload(row)
    channel = (row["target_collection"] or "default").upper().replace("-", "_")
    webhook_url = (
        payload.get("webhook_url")
        or os.environ.get(f"EXTERNAL_WEBHOOK_URL_{channel}")
        or os.environ.get("EXTERNAL_WEBHOOK_URL_DEFAULT")
        # Back-compat alias — accept the legacy env names for one release.
        or os.environ.get(f"ACTIVEPIECES_WEBHOOK_URL_{channel}")
        or os.environ.get("ACTIVEPIECES_WEBHOOK_URL_DEFAULT")
    )
    if not webhook_url:
        return "permanent_failure", (
            f"no external webhook configured for channel '{channel}'"
        )

    body = {
        "workspace_id":    str(row["workspace_id"]) if row["workspace_id"] else None,
        "source_schema":   row["source_schema"],
        "source_table":    row["source_table"],
        "source_id":       str(row["source_id"]) if row["source_id"] else None,
        "operation":       row["operation"],
        "idempotency_key": row["idempotency_key"],
        "payload":         payload,
    }
    body_bytes = json.dumps(body, sort_keys=True, default=str).encode("utf-8")

    secret = (
        os.environ.get("EXTERNAL_WEBHOOK_HMAC_SECRET")
        or os.environ.get("ACTIVEPIECES_HMAC_SECRET", "")  # back-compat
    ).encode("utf-8")
    sig = (
        "sha256=" + hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
        if secret else ""
    )

    headers = {
        "Content-Type":              "application/json",
        "X-GeoRAG-Idempotency-Key":  row["idempotency_key"] or "",
        "X-GeoRAG-Source":           f"{row['source_schema']}.{row['source_table']}",
    }
    if sig:
        headers["X-GeoRAG-Signature"] = sig

    timeout_s = float(
        os.environ.get("EXTERNAL_WEBHOOK_HTTP_TIMEOUT_S")
        or os.environ.get("ACTIVEPIECES_HTTP_TIMEOUT_S", "10"),
    )
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(webhook_url, content=body_bytes, headers=headers)
        if 200 <= resp.status_code < 300:
            return "success", None
        # 4xx (other than 408/429) is a permanent failure — payload-shape
        # bug we shouldn't retry. 5xx + 408/429 are transient.
        if resp.status_code in (408, 429) or 500 <= resp.status_code < 600:
            return "transient_failure", (
                f"external webhook returned HTTP {resp.status_code}: {resp.text[:200]}"
            )
        return "permanent_failure", (
            f"external webhook returned HTTP {resp.status_code}: {resp.text[:200]}"
        )
    except Exception as exc:  # noqa: BLE001
        return "transient_failure", f"{type(exc).__name__}: {exc}"


_DISPATCHERS = {
    "qdrant": _dispatch_qdrant,
    "neo4j": _dispatch_neo4j,
    "seaweedfs": _dispatch_seaweedfs,
    "external_webhook": _dispatch_external_webhook,
    # Back-compat alias so unmigrated outbox rows still route. Drop after
    # one release once `target_store='activepieces'` is no longer in the
    # pending_propagations table.
    "activepieces": _dispatch_external_webhook,
    "kestra": _dispatch_external_webhook,
}


# ---------------------------------------------------------------------------
# Claim + record loop.
# ---------------------------------------------------------------------------
_CLAIM_SQL = """
UPDATE outbox.pending_propagations
   SET status = 'in_flight', last_attempted_at = now()
 WHERE id IN (
        SELECT id FROM outbox.pending_propagations
         WHERE status = 'pending'
         ORDER BY enqueued_at
         FOR UPDATE SKIP LOCKED
         LIMIT $1
   )
RETURNING id, workspace_id, source_schema, source_table, source_id,
          target_store, target_collection, operation, payload,
          idempotency_key, target_store_concurrency_hint
"""


async def _record_attempt_and_advance(
    pool: asyncpg.Pool,
    row: asyncpg.Record,
    status: str,
    error_message: str | None,
    dead_letter_after: int,
) -> str:
    """Insert a propagation_attempts row + advance pending_propagations.

    Returns one of: ``success``, ``transient_failure``, ``dead_lettered``.
    """
    target = row["target_store"]
    async with pool.acquire() as conn:
        async with conn.transaction():
            attempt_no = (
                await conn.fetchval(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 "
                    "FROM outbox.propagation_attempts WHERE propagation_id = $1",
                    row["id"],
                )
            ) or 1

            audit = await emit_audit(
                conn,
                action_type=f"outbox.dispatch.{status}",
                workspace_id=row["workspace_id"],
                actor_kind="workflow",
                target_schema="outbox",
                target_table="pending_propagations",
                target_id=str(row["id"]),
                payload={
                    "target_store": target,
                    "operation": row["operation"],
                    "attempt_no": attempt_no,
                    "error": error_message,
                },
            )

            error_kind = None
            if status == "transient_failure":
                error_kind = "transient"
            elif status == "permanent_failure":
                error_kind = "permanent"

            await conn.execute(
                """
                INSERT INTO outbox.propagation_attempts(
                    propagation_id, workspace_id, attempt_no, status,
                    error_kind, error_message, finished_at, audit_ledger_ref
                ) VALUES ($1, $2, $3, $4, $5, $6, now(), $7)
                """,
                row["id"],
                row["workspace_id"],
                attempt_no,
                status,
                error_kind,
                error_message,
                audit.id,
            )

            if status == "success":
                await conn.execute(
                    "UPDATE outbox.pending_propagations "
                    "   SET status='succeeded', succeeded_at=now() WHERE id=$1",
                    row["id"],
                )
                return "success"

            if status == "permanent_failure" or attempt_no >= dead_letter_after:
                await conn.execute(
                    "UPDATE outbox.pending_propagations "
                    "   SET status='dead_lettered', dead_lettered_at=now() WHERE id=$1",
                    row["id"],
                )
                # silver.store_reconciliation_findings.workspace_id is NOT NULL
                # + has FK to silver.workspaces — only write a finding for
                # workspace-scoped propagations.
                if row["workspace_id"] is not None:
                    await conn.execute(
                        """
                        INSERT INTO silver.store_reconciliation_findings(
                            workspace_id, drift_type, severity,
                            source_store, target_store,
                            source_id, target_id, details, discovered_by
                        ) VALUES (
                            $1, 'outbox_dead_letter', 'medium',
                            'postgres', $2,
                            $3, $4, $5::jsonb, 'outbox_dispatcher'
                        )
                        """,
                        row["workspace_id"],
                        target,
                        row["source_id"],
                        row["idempotency_key"],
                        json.dumps(
                            {
                                "propagation_id": str(row["id"]),
                                "source_schema": row["source_schema"],
                                "source_table": row["source_table"],
                                "operation": row["operation"],
                                "attempts": attempt_no,
                                "last_error": error_message,
                            }
                        ),
                    )
                return "dead_lettered"

            # transient — release back to pending for the next pass.
            await conn.execute(
                "UPDATE outbox.pending_propagations SET status='pending' WHERE id=$1",
                row["id"],
            )
            return "transient_failure"


async def _dispatch_one(
    pool: asyncpg.Pool, row: asyncpg.Record, dead_letter_after: int
) -> str:
    target = row["target_store"]
    dispatcher = _DISPATCHERS.get(target)
    if dispatcher is None:
        return await _record_attempt_and_advance(
            pool, row, "permanent_failure", f"unknown target_store {target!r}",
            dead_letter_after,
        )

    sem = _semaphore_for(target, row["target_store_concurrency_hint"])
    async with sem:
        status, err = await dispatcher(row)

    return await _record_attempt_and_advance(pool, row, status, err, dead_letter_after)


@outbox_dispatcher.task(execution_timeout="2m")
async def drain(
    input: OutboxDispatcherInput, ctx: Context
) -> OutboxDispatcherOutput:
    started = time.monotonic()
    pool = await asyncpg.create_pool(
        _build_dsn(),
        min_size=2,
        max_size=10,
        statement_cache_size=0,
    )

    rows_processed = 0
    rows_succeeded = 0
    rows_transient = 0
    rows_dead = 0

    try:
        while time.monotonic() - started < input.max_runtime_seconds:
            async with pool.acquire() as conn:
                rows = await conn.fetch(_CLAIM_SQL, input.batch_size)

            if not rows:
                await asyncio.sleep(input.poll_interval_seconds)
                continue

            results = await asyncio.gather(
                *[
                    _dispatch_one(pool, r, input.dead_letter_after_attempts)
                    for r in rows
                ],
                return_exceptions=False,
            )
            rows_processed += len(rows)
            rows_succeeded += sum(1 for r in results if r == "success")
            rows_transient += sum(1 for r in results if r == "transient_failure")
            rows_dead += sum(1 for r in results if r == "dead_lettered")
    finally:
        await pool.close()

    # Phase 5 admin surface push — drives Admin/ExportGate. Only fires
    # when the dispatcher actually processed rows (silent ticks add noise
    # without value).
    if rows_processed > 0:
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            import logging
            admin_payload = {
                "workflow_kind": "outbox_dispatcher",
                "rows_processed": rows_processed,
                "rows_succeeded": rows_succeeded,
                "rows_transient_failed": rows_transient,
                "rows_dead_lettered": rows_dead,
                "status": "success",
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="export-gate",
                affected_props=["results"],
                payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "outbox_dispatcher: admin surface broadcasts failed err=%s", exc,
            )

    return OutboxDispatcherOutput(
        rows_processed=rows_processed,
        rows_succeeded=rows_succeeded,
        rows_transient_failed=rows_transient,
        rows_dead_lettered=rows_dead,
        elapsed_seconds=time.monotonic() - started,
    )


__all__ = [
    "outbox_dispatcher",
    "OutboxDispatcherInput",
    "OutboxDispatcherOutput",
]
