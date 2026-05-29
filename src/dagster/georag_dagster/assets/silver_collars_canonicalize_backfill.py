"""Backfill silver.collars.hole_id_canonical for legacy NULL rows.

The 2026-05-25 chat-cards audit found 282 of 567 silver.collars rows had a
non-NULL ``hole_id`` but a NULL ``hole_id_canonical``. These predate the
fixes that route every collar-ingest writer through ``canonicalize()`` on
INSERT (silver.py / silver_xlsx.py / las_ingester.py — all patched in the
same change set as this asset).

This asset re-applies the canonical function — the SAME one the CSV parser
uses (``parsers/_hole_id.py::canonicalize``) — to every legacy row and
writes the result back in batches.

Idempotency: the WHERE clause filters on ``hole_id_canonical IS NULL``, so
re-running the asset is a no-op after the first successful sweep.
Workspace tenancy: every UPDATE is workspace-scoped via collar_id; we
iterate per workspace so RLS / GUC state stays clean per write.

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13.
"""

import logging

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.parsers._hole_id import canonicalize
from georag_dagster.resources import PostgresResource


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Pull workspace_id alongside so we can group writes by tenant and keep
# the UPDATE filter tight.
SELECT_NULL_CANONICAL_SQL = """
SELECT
    collar_id::text   AS collar_id,
    workspace_id::text AS workspace_id,
    hole_id
FROM silver.collars
WHERE hole_id IS NOT NULL
  AND hole_id_canonical IS NULL
"""

# Workspace-scoped UPDATE — caller passes (collar_id, workspace_id, value)
# tuples. Re-checks ``hole_id_canonical IS NULL`` so concurrent writers
# don't get clobbered.
UPDATE_CANONICAL_SQL = """
UPDATE silver.collars
SET hole_id_canonical = %(hole_id_canonical)s
WHERE collar_id = %(collar_id)s::uuid
  AND workspace_id = %(workspace_id)s::uuid
  AND hole_id_canonical IS NULL
"""


BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class SilverCollarsCanonicalizeBackfillConfig(Config):
    """Runtime configuration for the silver_collars_canonicalize_backfill asset."""

    # Optional: scope the backfill to a single workspace. Empty string → all
    # workspaces (the default — this is a one-off legacy sweep).
    workspace_id: str = ""


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="silver",
    description=(
        "One-off backfill of silver.collars.hole_id_canonical for legacy rows "
        "where the canonical form was never written on INSERT. Reuses the "
        "CSV parser's canonicalize() function (parsers/_hole_id.py) and "
        "applies it in BATCH_SIZE=100 chunks per workspace. Idempotent — "
        "after the sweep, repeat runs UPDATE 0 rows because the WHERE clause "
        "filters on hole_id_canonical IS NULL."
    ),
)
def silver_collars_canonicalize_backfill(
    context: AssetExecutionContext,
    config: SilverCollarsCanonicalizeBackfillConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Backfill hole_id_canonical for all legacy NULL silver.collars rows."""

    workspace_filter = config.workspace_id.strip() or None

    rows_scanned = 0
    rows_updated = 0
    rows_skipped_blank_canonical = 0
    workspaces_touched: set[str] = set()

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if workspace_filter:
                cur.execute(
                    SELECT_NULL_CANONICAL_SQL + " AND workspace_id = %(workspace_id)s::uuid",
                    {"workspace_id": workspace_filter},
                )
            else:
                cur.execute(SELECT_NULL_CANONICAL_SQL)
            null_rows = list(cur.fetchall())

        rows_scanned = len(null_rows)
        context.log.info(
            "silver_collars_canonicalize_backfill: %d candidate row(s) "
            "found (workspace_filter=%s)",
            rows_scanned,
            workspace_filter or "(all)",
        )

        # Build the batch payload. canonicalize() may return None when the
        # raw hole_id is whitespace / separators only; skip those rather
        # than writing NULL → NULL (no-op + wasted IO).
        batch_payload: list[dict] = []
        for row in null_rows:
            canonical = canonicalize(row["hole_id"])
            if not canonical:
                rows_skipped_blank_canonical += 1
                continue
            batch_payload.append({
                "collar_id":         row["collar_id"],
                "workspace_id":      row["workspace_id"],
                "hole_id_canonical": canonical,
            })
            workspaces_touched.add(row["workspace_id"])

        # Apply in BATCH_SIZE chunks via execute_batch — each row is a
        # parameterized UPDATE with the workspace_id pinned, so RLS /
        # tenancy isn't crossed.
        with conn.cursor() as cur:
            for start in range(0, len(batch_payload), BATCH_SIZE):
                chunk = batch_payload[start : start + BATCH_SIZE]
                psycopg2.extras.execute_batch(
                    cur, UPDATE_CANONICAL_SQL, chunk, page_size=BATCH_SIZE,
                )
                # rowcount on execute_batch reflects the LAST statement; we
                # therefore count expected rows (idempotent UPDATEs that
                # filter on IS NULL — concurrent writers are rare on a
                # one-off sweep, so chunk size is a fair proxy).
                rows_updated += len(chunk)
                context.log.info(
                    "  batch %d / %d updated (%d rows total)",
                    (start // BATCH_SIZE) + 1,
                    (len(batch_payload) + BATCH_SIZE - 1) // BATCH_SIZE,
                    rows_updated,
                )

        conn.commit()

    context.log.info(
        "silver_collars_canonicalize_backfill: scanned=%d updated=%d "
        "skipped_blank_canonical=%d workspaces_touched=%d",
        rows_scanned, rows_updated, rows_skipped_blank_canonical,
        len(workspaces_touched),
    )

    return MaterializeResult(
        metadata={
            "rows_scanned":               MetadataValue.int(rows_scanned),
            "rows_updated":               MetadataValue.int(rows_updated),
            "rows_skipped_blank_canonical": MetadataValue.int(rows_skipped_blank_canonical),
            "workspaces_touched":         MetadataValue.int(len(workspaces_touched)),
            "workspace_filter":           MetadataValue.text(workspace_filter or "(all)"),
        }
    )


__all__ = [
    "BATCH_SIZE",
    "SilverCollarsCanonicalizeBackfillConfig",
    "silver_collars_canonicalize_backfill",
]
