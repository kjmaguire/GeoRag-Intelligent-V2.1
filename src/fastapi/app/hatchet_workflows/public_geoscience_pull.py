"""Phase 2 Step 4 — ``public_geoscience_pull`` Hatchet workflow.

The external integration side runs on a cron, calls a public ArcGIS / WFS feed,
drops the response GeoJSON to bronze under a versioned key, then POSTs
to ``/internal/v1/integrations/public_geoscience_pull/trigger`` with
``{minio_key, source_id, source_url, fetched_at}``. This workflow:

  1. checks the platform feature flag ``activepieces.public_geoscience_pull.enabled``;
  2. reads the S3 object, validates it's parseable GeoJSON, counts
     features, hashes the bytes;
  3. records the pull in ``bronze.provenance`` so downstream silver
     materialisations can pair against it;
  4. emits ``public_geo.pull.complete`` to ``audit.audit_ledger``.

Idempotent on ``source_file_sha256`` — re-runs against the same S3
object skip the provenance insert (informational re-emit only).

Pool: ``ai`` (no heavy ingest — just validate + register).
Action: ``public_geoscience_pull``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime
from typing import Any

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet


log = logging.getLogger("georag.hatchet.public_geoscience_pull")


# =============================================================================
# IO models
# =============================================================================
class PublicGeoSciencePullInput(BaseModel):
    """Sent by Kestra' HTTP piece. Kestra is the integration
    edge — this workflow does NOT call upstream itself; the flow is
    responsible for fetching + dropping into S3."""

    minio_key: str = Field(..., description="S3 key under bronze/")
    source_id: str = Field(..., description="public_geo source identifier")
    source_url: str | None = Field(default=None, description="Upstream URL (informational, recorded only)")
    fetched_at: str | None = Field(default=None, description="ISO-8601 timestamp Kestra stamped on fetch")


class PublicGeoSciencePullOut(BaseModel):
    skipped: bool = False
    reason: str | None = None
    minio_key: str
    source_id: str
    sha256: str | None = None
    feature_count: int | None = None
    provenance_id: str | None = None
    duration_ms: int


# =============================================================================
# Helpers
# =============================================================================
def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _s3_endpoint() -> str:
    return os.environ.get(
        "S3_ENDPOINT_URL",
        os.environ.get("MINIO_ENDPOINT", "http://minio:8333"),
    )


def _s3_credentials() -> tuple[str, str]:
    return (
        os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("MINIO_ROOT_USER", "georag-admin"),
        os.environ.get("AWS_SECRET_ACCESS_KEY")
        or os.environ.get("MINIO_ROOT_PASSWORD", ""),
    )


async def _download_from_s3(minio_key: str) -> bytes:
    import aioboto3
    sess = aioboto3.Session(
        aws_access_key_id=_s3_credentials()[0],
        aws_secret_access_key=_s3_credentials()[1],
        region_name="us-east-1",
    )
    bucket = os.environ.get("MINIO_BUCKET_BRONZE", "bronze")
    async with sess.client("s3", endpoint_url=_s3_endpoint()) as s3:
        resp = await s3.get_object(Bucket=bucket, Key=minio_key)
        return await resp["Body"].read()


async def _flag_enabled(conn: asyncpg.Connection) -> bool:
    # Phase 3 Step 3 — namespace renamed to orchestrator-neutral
    # `flows.<flow>.enabled`. The migration mirrors values from the
    # old activepieces.* row; Step 7 drops the old rows.
    row = await conn.fetchrow(
        """
        SELECT bool_value
          FROM workspace.feature_flags
         WHERE workspace_id IS NULL
           AND flag_name = 'flows.public_geoscience_pull.enabled'
        """
    )
    return bool(row and row["bool_value"])


def _validate_geojson(body: bytes) -> tuple[bool, int, str | None]:
    """Return (ok, feature_count, error)."""
    try:
        doc = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return False, 0, f"not valid JSON: {e}"
    if not isinstance(doc, dict):
        return False, 0, "top-level is not a JSON object"
    t = doc.get("type")
    if t == "FeatureCollection":
        feats = doc.get("features") or []
        if not isinstance(feats, list):
            return False, 0, "FeatureCollection.features is not an array"
        return True, len(feats), None
    if t == "Feature":
        return True, 1, None
    return False, 0, f"unsupported GeoJSON type: {t}"


# =============================================================================
# Workflow
# =============================================================================
public_geoscience_pull = hatchet.workflow(
    name="public_geoscience_pull",
    input_validator=PublicGeoSciencePullInput,
)


@public_geoscience_pull.task(execution_timeout="2m", retries=1)
async def pull(
    input: PublicGeoSciencePullInput, ctx: Context
) -> PublicGeoSciencePullOut:
    t_start = time.monotonic()

    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0,
    )
    try:
        async with pool.acquire() as conn:
            if not await _flag_enabled(conn):
                return PublicGeoSciencePullOut(
                    skipped=True,
                    reason="feature flag flows.public_geoscience_pull.enabled = false",
                    minio_key=input.minio_key,
                    source_id=input.source_id,
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                )

            log.info(
                "public_geoscience_pull start key=%s source=%s",
                input.minio_key, input.source_id,
            )
            body = await _download_from_s3(input.minio_key)
            sha256 = hashlib.sha256(body).hexdigest()
            ok, feature_count, err = _validate_geojson(body)
            if not ok:
                # Record the failure to bronze.provenance so the operator
                # surface (Step 6) can see it; classify-and-write at the
                # same site keeps the retry-loop simple.
                raise ValueError(f"public_geoscience_pull: {err}")

            # Idempotency — if a provenance row already exists for this
            # sha256, return it without inserting again. The dashboard
            # surface reads `created_at` to detect re-pulls of identical
            # content.
            async with conn.transaction():
                existing = await conn.fetchrow(
                    """
                    SELECT provenance_id::text AS pid
                      FROM bronze.provenance
                     WHERE source_file_sha256 = $1
                       AND parser_name        = 'activepieces_public_geoscience_pull'
                     ORDER BY ingested_at DESC
                     LIMIT 1
                    """,
                    sha256,
                )
                if existing:
                    provenance_id = existing["pid"]
                    log.info(
                        "public_geoscience_pull idempotent skip — sha256 already recorded id=%s",
                        provenance_id,
                    )
                else:
                    target_id = str(uuid.uuid4())
                    provenance_id = await conn.fetchval(
                        """
                        INSERT INTO bronze.provenance
                            (target_schema, target_table, target_id,
                             source_file, source_file_sha256,
                             parser_name, parser_version, ingest_run_id,
                             source_col_map)
                        VALUES ('bronze', 'public_geoscience_raw', $1::uuid,
                                $2, $3,
                                'activepieces_public_geoscience_pull', '1', $4::uuid,
                                $5::jsonb)
                        RETURNING provenance_id::text
                        """,
                        target_id,
                        f"s3://bronze/{input.minio_key}",
                        sha256,
                        ctx.workflow_run_id,
                        json.dumps({
                            "source_id": input.source_id,
                            "source_url": input.source_url,
                            "fetched_at": input.fetched_at,
                            "feature_count": feature_count,
                        }),
                    )

            try:
                await emit_audit(
                    conn,
                    action_type="public_geo.pull.complete",
                    workspace_id=None,
                    actor_id=None,
                    actor_kind="workflow",
                    target_schema="bronze",
                    target_table="provenance",
                    target_id=provenance_id,
                    payload={
                        "minio_key": input.minio_key,
                        "source_id": input.source_id,
                        "source_url": input.source_url,
                        "fetched_at": input.fetched_at,
                        "sha256": sha256,
                        "feature_count": feature_count,
                        "idempotent_skip": existing is not None,
                    },
                    trace_id=ctx.workflow_run_id,
                )
            except Exception as e:
                log.warning("public_geoscience_pull audit emit failed: %s", e)

            # Phase 4 — invalidate the browser-side PGEO tile cache so
            # PublicGeoscienceMap re-fetches the freshly-pulled features
            # without waiting for the (1h–24h) Cache-Control TTL to lapse.
            #
            # Skip on idempotent re-pulls (no content change → no cache to
            # bust) AND when feature_count == 0 (nothing landed → existing
            # tiles still correct).
            #
            # Reads the post-write MAX(updated_at) directly so the broadcast
            # carries the same epoch value TileProxyController will compute
            # for the next tile fetch's ETag. Falls back to wall-clock
            # epoch when the jurisdictions table isn't updated by the pull
            # (the `?v=` cache-bust still does its job; the server ETag will
            # match the post-write value on the next request).
            should_broadcast = existing is None and feature_count > 0
            if should_broadcast:
                try:
                    epoch_row = await conn.fetchrow(
                        "SELECT EXTRACT(EPOCH FROM MAX(updated_at))::bigint AS epoch_s "
                        "FROM public_geo.jurisdictions",
                    )
                    epoch_s = int(epoch_row["epoch_s"]) if epoch_row and epoch_row["epoch_s"] else int(time.time())

                    from app.services.laravel_bridge import post_public_geoscience_tiles_invalidated
                    await post_public_geoscience_tiles_invalidated(
                        jurisdiction_epoch=epoch_s,
                        # source_ids omitted — a public_geoscience_pull may
                        # touch any of the 8 PGEO views; let the receiver
                        # invalidate them all. SMDI overnight (P3) will pass
                        # source_ids=['smdi_deposits'] when it lands.
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "public_geoscience_pull: tile invalidation broadcast failed "
                        "key=%s err=%s", input.minio_key, exc,
                    )

    finally:
        await pool.close()

    duration_ms = int((time.monotonic() - t_start) * 1000)
    return PublicGeoSciencePullOut(
        skipped=False,
        reason=None,
        minio_key=input.minio_key,
        source_id=input.source_id,
        sha256=sha256,
        feature_count=feature_count,
        provenance_id=provenance_id,
        duration_ms=duration_ms,
    )


__all__ = [
    "public_geoscience_pull",
    "PublicGeoSciencePullInput",
    "PublicGeoSciencePullOut",
]
