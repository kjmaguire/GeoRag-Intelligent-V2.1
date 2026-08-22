"""§11.3 wave 1 helper — restore PG rows from a workspace_export manifest.

Reads a JSONL.gz object produced by ``app.hatchet_workflows.workspace_export``
and INSERTs the rows back into their original PG tables under the target
workspace's RLS scope. Supports two URI schemes:

  - file:// — for local testing (the integration test uses this)
  - s3://   — for production (workspace_export writes to SeaweedFS S3)

Idempotency
-----------

Each INSERT uses ``ON CONFLICT (id) DO NOTHING`` for PK collisions. This
makes the restore safe to re-run — it will not duplicate rows that
already exist + will not overwrite rows that may have been edited since
the export was taken.

For production "atomic restore" semantics (drop + insert vs. merge),
the operator runs the restore against a fresh target database, not the
source DB.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
from typing import Any
from urllib.parse import urlparse

import asyncpg

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn

log = logging.getLogger("georag.hatchet._restore_pg_from_export")


_TABLE_KEY_TO_QUALIFIED = {
    "silver_workspaces":                "silver.workspaces",
    "silver_hypotheses":                "silver.hypotheses",
    "silver_decision_records":          "silver.decision_records",
    "silver_answer_runs":               "silver.answer_runs",
    "silver_evidence_items":            "silver.evidence_items",
    "silver_document_passages":         "silver.document_passages",
    "audit_ledger_anchors":             "audit.audit_ledger",
    "targeting_target_recommendations": "targeting.target_recommendations",
    "ops_support_tickets":              "ops.support_tickets",
}


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_build_dsn = build_dsn


async def _fetch_manifest_bytes(manifest_uri: str) -> bytes:
    """Resolve file:// or s3:// scheme + return gzipped bytes."""
    parsed = urlparse(manifest_uri)
    if parsed.scheme == "file":
        with open(parsed.path, "rb") as f:
            return f.read()
    if parsed.scheme == "s3":
        # bucket comes straight out of the s3:// URI's netloc — genuinely
        # dynamic, not one of georag_object_storage's four fixed logical
        # Bucket members, so this uses the raw-client escape hatch
        # (async_client_kwargs) rather than the higher-level
        # AsyncObjectStorage interface.
        import aioboto3
        from georag_object_storage import StorageConfig, async_client_kwargs

        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        session = aioboto3.Session()
        async with session.client("s3", **async_client_kwargs(StorageConfig.from_env())) as s3:
            resp = await s3.get_object(Bucket=bucket, Key=key)
            return await resp["Body"].read()
    raise ValueError(f"unsupported manifest_uri scheme: {parsed.scheme!r}")


def _parse_jsonl_gz(body: bytes) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """Return (manifest_dict, [(table_key, row_dict), ...]).
    The first JSONL line is the manifest; subsequent lines are tagged rows."""
    with gzip.GzipFile(fileobj=io.BytesIO(body), mode="rb") as gz:
        text = gz.read().decode("utf-8")
    lines = text.splitlines()
    if not lines:
        raise ValueError("empty manifest body")
    manifest = json.loads(lines[0])
    rows: list[tuple[str, dict[str, Any]]] = []
    for line in lines[1:]:
        if not line:
            continue
        entry = json.loads(line)
        rows.append((entry["table"], entry["row"]))
    return manifest, rows


async def _insert_row(
    conn: asyncpg.Connection, qualified_table: str, row: dict[str, Any],
) -> int:
    """Insert one row with ON CONFLICT (id) DO NOTHING. Returns 1 if
    inserted, 0 if conflict-skipped or no columns matched."""
    if not row:
        return 0
    # Drop columns the target table doesn't have (defensive — export
    # might be from a slightly older schema).
    schema_name, table_name = qualified_table.split(".", 1)
    valid_cols = await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = $2",
        schema_name, table_name,
    )
    valid = {c["column_name"] for c in valid_cols}
    filtered = {k: v for k, v in row.items() if k in valid}
    if not filtered:
        return 0

    cols = list(filtered.keys())
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
    col_list = ", ".join(f'"{c}"' for c in cols)
    sql = (
        f"INSERT INTO {qualified_table} ({col_list}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO NOTHING"
    )
    try:
        result = await conn.execute(sql, *[filtered[c] for c in cols])
    except asyncpg.exceptions.UndefinedColumnError:
        # No `id` column on the target → some tables (audit_ledger) use
        # composite keys. Skip the conflict clause entirely.
        sql_noconflict = (
            f"INSERT INTO {qualified_table} ({col_list}) "
            f"VALUES ({placeholders})"
        )
        try:
            result = await conn.execute(sql_noconflict, *[filtered[c] for c in cols])
        except Exception as exc2:  # noqa: BLE001
            log.debug(
                "_insert_row: %s row insert failed (%s); skipping",
                qualified_table, exc2,
            )
            return 0
    except Exception as exc:  # noqa: BLE001
        log.debug(
            "_insert_row: %s row insert failed (%s); skipping",
            qualified_table, exc,
        )
        return 0

    return 1 if result.startswith("INSERT") and not result.endswith(" 0") else 0


async def restore_postgres_from_export(
    workspace_id: str, manifest_uri: str,
) -> dict[str, Any]:
    """Top-level PG restore. Returns a dict with restored row counts.

    Raises ValueError if the manifest's workspace_id doesn't match the
    target workspace (refuses cross-workspace restore — operator must
    re-export with the correct workspace_id if the intent is clone).
    """
    body = await _fetch_manifest_bytes(manifest_uri)
    manifest, tagged_rows = _parse_jsonl_gz(body)

    if manifest.get("format") != "workspace_export":
        raise ValueError(
            f"manifest is not a workspace_export "
            f"(format={manifest.get('format')!r})"
        )
    mani_ws = manifest.get("workspace_id")
    if mani_ws and mani_ws != workspace_id:
        raise ValueError(
            f"manifest workspace_id={mani_ws} does not match target "
            f"workspace_id={workspace_id} (refusing cross-workspace restore)"
        )

    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    rows_inserted: dict[str, int] = {}
    tables_touched: list[str] = []
    try:
        # Set RLS scope so writes land under the target workspace.
        #
        # is_local=False: this is a dedicated asyncpg.connect() (above)
        # with no wrapping transaction -- the per-row SAVEPOINTs below are
        # opened individually, not under one outer txn. SET LOCAL would be
        # discarded here, so the scope these writes depend on was never
        # actually applied.
        await bind_workspace_scope(
            conn, workspace_id=workspace_id,
            site="hatchet.restore_pg_from_export", is_local=False,
        )
        # Per-row SAVEPOINTs so a single constraint violation (e.g.
        # FK to a workspace that exists at export time but not at
        # restore time) doesn't poison the whole transaction. This
        # mirrors pg_restore's --single-transaction=false behaviour.
        for table_key, row in tagged_rows:
            qualified = _TABLE_KEY_TO_QUALIFIED.get(table_key)
            if qualified is None:
                log.debug(
                    "restore_postgres_from_export: unknown table_key %r; skipping row",
                    table_key,
                )
                continue
            if qualified not in tables_touched:
                tables_touched.append(qualified)
            try:
                async with conn.transaction():
                    n = await _insert_row(conn, qualified, row)
                    rows_inserted[qualified] = rows_inserted.get(qualified, 0) + n
            except Exception as exc:  # noqa: BLE001
                # Row insert raised AFTER the helper's own try/except
                # absorbed it — e.g. FK violation. Log + move on.
                log.debug(
                    "restore_postgres_from_export: %s row dropped (%s)",
                    qualified, exc,
                )
                continue
    finally:
        await conn.close()

    log.info(
        "restore_postgres_from_export ws=%s tables=%d rows=%d",
        workspace_id, len(tables_touched), sum(rows_inserted.values()),
    )
    return {
        "manifest_workspace_id": mani_ws,
        "tables":                tables_touched,
        "rows_inserted":         rows_inserted,
        "total_rows_inserted":   sum(rows_inserted.values()),
    }


__all__ = ["restore_postgres_from_export"]
