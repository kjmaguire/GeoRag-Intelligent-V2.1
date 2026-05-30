"""Plan §6a — silver.data_quality_flags writer helper.

The data_quality_flags design doc spec'd a single helper that all 5
rule families (assay validation, collar validation, interval overlap,
unit consistency, CRS consistency) use to upsert flag rows. This is
the helper.

Key contract from the design (§ "Where flags are written"):
  * Idempotent by `(workspace_id, record_type, record_id, flag_type,
    rule_version)` — re-running the same rule against the same row
    must NOT create a duplicate flag.
  * Workspace-scoped via the RLS GUC `app.workspace_id`; the helper
    sets the GUC inside its own transaction.
  * Optional fields (project_id, source_document_id, source_page,
    source_row_range, rule_id, threshold_payload) all flow through.
  * Severity is one of INFO / WARNING / ERROR (CHECK on the column).
  * record_type is one of the 14 CHECK-allowed values from the migration.

Pure-async; no Dagster dependencies — designed to be importable from
Dagster ops (`flag_writer.upsert_flag(...)`) AND from FastAPI tools
(future: if/when a runtime validator surfaces a flag during a query).

Best-effort: any DB failure logs at WARNING + returns False; callers
should treat the result as advisory (flag write failures should not
block the data pipeline).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import asyncpg


logger = logging.getLogger(__name__)


__all__ = [
    "DataQualityFlag",
    "upsert_flag",
    "upsert_flags",
    "upsert_flag_sync",
    "upsert_flags_sync",
    "ALLOWED_RECORD_TYPES",
    "ALLOWED_SEVERITIES",
]


# Mirror of the CHECK constraints on silver.data_quality_flags. The
# helper validates inputs against these BEFORE hitting the DB so a
# typo surfaces as a clear ValueError instead of a CheckViolationError
# buried in an asyncpg traceback.
ALLOWED_RECORD_TYPES: frozenset[str] = frozenset({
    "assay_interval",
    "collar",
    "survey_point",
    "lithology_interval",
    "alteration_interval",
    "mineralization_interval",
    "structural_interval",
    "downhole_geophysics_point",
    "composite_interval",
    "document_chunk",
    "table_extraction",
    "spatial_feature",
    "sample",
    "geochronology_sample",
})

ALLOWED_SEVERITIES: frozenset[str] = frozenset({"INFO", "WARNING", "ERROR"})


@dataclass(frozen=True)
class DataQualityFlag:
    """Input shape for :func:`upsert_flag`.

    Required fields are positional; everything else is keyword-only +
    optional. The dataclass is frozen so a batch list (passed to
    :func:`upsert_flags`) can't be mutated mid-iteration.
    """

    workspace_id: str
    record_type: str
    record_id: str
    flag_type: str
    severity: str
    description: str
    project_id: str | None = None
    source_document_id: str | None = None
    source_page: int | None = None
    source_row_range: str | None = None
    rule_id: str | None = None
    rule_version: str | None = None
    threshold_payload: dict[str, Any] | None = None


def _validate(flag: DataQualityFlag) -> None:
    """Argument-shape validation. Catches typos before the DB does.

    Raises:
        ValueError: when severity / record_type isn't in the allowed
            sets, or required strings are empty.
    """
    if flag.severity not in ALLOWED_SEVERITIES:
        raise ValueError(
            f"severity={flag.severity!r} not in {sorted(ALLOWED_SEVERITIES)}"
        )
    if flag.record_type not in ALLOWED_RECORD_TYPES:
        raise ValueError(
            f"record_type={flag.record_type!r} not in "
            f"{sorted(ALLOWED_RECORD_TYPES)}"
        )
    if not flag.workspace_id:
        raise ValueError("workspace_id is required")
    if not flag.record_id:
        raise ValueError("record_id is required")
    if not flag.flag_type:
        raise ValueError("flag_type is required")
    if not flag.description:
        raise ValueError("description is required")


# Upsert key matches the design doc's idempotency contract:
# (workspace_id, record_type, record_id, flag_type, rule_version).
# When rule_version is NULL the key collapses to a 4-tuple — the
# COALESCE in WHERE handles that case explicitly.
_UPSERT_SQL = """
    WITH upsert AS (
        UPDATE silver.data_quality_flags
        SET severity = $5,
            description = $6,
            source_document_id = $7::uuid,
            source_page = $8,
            source_row_range = $9,
            rule_id = $10,
            threshold_payload = $11::jsonb,
            project_id = $12::uuid,
            flagged_at = NOW(),
            -- A re-emit clears the resolution lifecycle: the rule is
            -- saying "this is still a problem" so the SME flow restarts.
            reviewed_by_user_id = NULL,
            reviewed_at = NULL,
            resolved_at = NULL,
            resolution = NULL,
            resolution_notes = NULL
        WHERE workspace_id = $1::uuid
          AND record_type = $2
          AND record_id = $3
          AND flag_type = $4
          AND rule_version IS NOT DISTINCT FROM $13
        RETURNING flag_id
    )
    INSERT INTO silver.data_quality_flags (
        workspace_id, record_type, record_id, flag_type,
        severity, description,
        source_document_id, source_page, source_row_range,
        rule_id, threshold_payload, project_id, rule_version,
        flagged_at, flagged_by
    )
    SELECT $1::uuid, $2, $3, $4,
           $5, $6,
           $7::uuid, $8, $9,
           $10, $11::jsonb, $12::uuid, $13,
           NOW(), 'system'
    WHERE NOT EXISTS (SELECT 1 FROM upsert)
"""


async def upsert_flag(
    conn: asyncpg.Connection,
    flag: DataQualityFlag,
) -> bool:
    """Idempotently insert or update one flag row.

    Args:
        conn: live asyncpg connection. Caller is responsible for
            connection lifecycle. The helper sets the
            ``app.workspace_id`` GUC inside this call so RLS lets
            the write through; the caller doesn't need to set it
            separately.
        flag: the flag to write.

    Returns:
        True on success, False when a DB-level error occurred (logged
        at WARNING). Validation errors raise ``ValueError`` directly
        because they're caller bugs, not runtime conditions.
    """
    _validate(flag)
    try:
        # Set the RLS GUC for this transaction so the workspace-scoped
        # policy lets the INSERT/UPDATE through. Using set_config with
        # is_local=true (3rd arg) so the setting auto-resets when the
        # tx closes — no cross-request leakage on a pooled connection.
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, true)",
            flag.workspace_id,
        )
        await conn.execute(
            _UPSERT_SQL,
            flag.workspace_id,
            flag.record_type,
            flag.record_id,
            flag.flag_type,
            flag.severity,
            flag.description,
            flag.source_document_id,
            flag.source_page,
            flag.source_row_range,
            flag.rule_id,
            _payload_jsonb(flag.threshold_payload),
            flag.project_id,
            flag.rule_version,
        )
        return True
    except Exception:
        logger.warning(
            "silver_dq_flag_writer.upsert_failed workspace=%s record=%s/%s "
            "flag_type=%s",
            flag.workspace_id, flag.record_type, flag.record_id,
            flag.flag_type, exc_info=True,
        )
        return False


async def upsert_flags(
    conn: asyncpg.Connection,
    flags: list[DataQualityFlag],
) -> int:
    """Batch variant — runs `upsert_flag` per row inside a single transaction.

    Returns the number of flags written successfully. A single-flag
    failure is logged but does not abort the batch (Dagster rule
    families can run dozens of independent checks per row; one
    failed insert shouldn't block the others).

    Yes, a single transaction means the GUC is set once. Each
    `upsert_flag` call re-sets it for safety but the per-call set
    is cheap.
    """
    if not flags:
        return 0

    written = 0
    async with conn.transaction():
        for flag in flags:
            if await upsert_flag(conn, flag):
                written += 1
    return written


def _payload_jsonb(payload: dict[str, Any] | None) -> str:
    """Serialise the threshold_payload dict for the JSONB column.

    asyncpg's JSONB codec wants a str (the driver json-encodes itself).
    None → '{}' so the DEFAULT column constraint stays satisfied.
    """
    import json as _json

    if payload is None:
        return "{}"
    return _json.dumps(payload)


# ---------------------------------------------------------------------------
# Sync variants — Dagster path uses psycopg2 via PostgresResource
# ---------------------------------------------------------------------------
#
# The async variants above are the canonical entry point for FastAPI
# callers. Dagster assets use a `PostgresResource` that hands back
# psycopg2 connections (see georag_dagster.resources). Rather than
# force every rule family to maintain its own SQL, expose sync
# variants that share the upsert string + validation logic.
#
# Same idempotency contract, same RLS GUC handling, same validation.
# The only difference is the cursor API + parameter style.

# psycopg2 uses %s placeholders, NOT $N. Build a parallel SQL constant
# rather than substituting at call time so the SQL is auditable.
_UPSERT_SQL_PSYCOPG2 = """
    WITH upsert AS (
        UPDATE silver.data_quality_flags
        SET severity = %(severity)s,
            description = %(description)s,
            source_document_id = %(source_document_id)s,
            source_page = %(source_page)s,
            source_row_range = %(source_row_range)s,
            rule_id = %(rule_id)s,
            threshold_payload = %(threshold_payload)s::jsonb,
            project_id = %(project_id)s,
            flagged_at = NOW(),
            reviewed_by_user_id = NULL,
            reviewed_at = NULL,
            resolved_at = NULL,
            resolution = NULL,
            resolution_notes = NULL
        WHERE workspace_id = %(workspace_id)s::uuid
          AND record_type = %(record_type)s
          AND record_id = %(record_id)s
          AND flag_type = %(flag_type)s
          AND rule_version IS NOT DISTINCT FROM %(rule_version)s
        RETURNING flag_id
    )
    INSERT INTO silver.data_quality_flags (
        workspace_id, record_type, record_id, flag_type,
        severity, description,
        source_document_id, source_page, source_row_range,
        rule_id, threshold_payload, project_id, rule_version,
        flagged_at, flagged_by
    )
    SELECT %(workspace_id)s::uuid, %(record_type)s, %(record_id)s, %(flag_type)s,
           %(severity)s, %(description)s,
           %(source_document_id)s, %(source_page)s, %(source_row_range)s,
           %(rule_id)s, %(threshold_payload)s::jsonb, %(project_id)s, %(rule_version)s,
           NOW(), 'system'
    WHERE NOT EXISTS (SELECT 1 FROM upsert)
"""


def upsert_flag_sync(conn: Any, flag: DataQualityFlag) -> bool:
    """Synchronous variant for Dagster / psycopg2 callers.

    Mirrors :func:`upsert_flag` exactly — same idempotency key, same
    GUC handling, same validation. The only difference is sync
    execution + psycopg2 cursor.

    Args:
        conn: a psycopg2 connection (typically from
            ``PostgresResource.get_connection()``).
        flag: the flag to write.

    Returns:
        True on success, False on DB-level error.
    """
    _validate(flag)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.workspace_id', %s, true)",
                (flag.workspace_id,),
            )
            cur.execute(_UPSERT_SQL_PSYCOPG2, {
                "workspace_id": flag.workspace_id,
                "record_type": flag.record_type,
                "record_id": flag.record_id,
                "flag_type": flag.flag_type,
                "severity": flag.severity,
                "description": flag.description,
                "source_document_id": flag.source_document_id,
                "source_page": flag.source_page,
                "source_row_range": flag.source_row_range,
                "rule_id": flag.rule_id,
                "threshold_payload": _payload_jsonb(flag.threshold_payload),
                "project_id": flag.project_id,
                "rule_version": flag.rule_version,
            })
        return True
    except Exception:
        logger.warning(
            "silver_dq_flag_writer.upsert_sync_failed workspace=%s record=%s/%s "
            "flag_type=%s",
            flag.workspace_id, flag.record_type, flag.record_id,
            flag.flag_type, exc_info=True,
        )
        return False


def upsert_flags_sync(conn: Any, flags: list[DataQualityFlag]) -> int:
    """Batch sync variant — same semantics as :func:`upsert_flags`.

    Uses a single transaction (psycopg2's autocommit=False default).
    Caller is responsible for the final ``conn.commit()`` so a Dagster
    asset can group flag-writes with the asset's own data writes in
    one atomic operation.
    """
    if not flags:
        return 0
    written = 0
    for flag in flags:
        if upsert_flag_sync(conn, flag):
            written += 1
    return written
