"""Plan §6a — silver.data_quality_flags writer (Dagster-side).

Dagster mirror of the sync helpers in
``src/fastapi/app/services/silver_dq_flag_writer.py``. Same idempotency
contract, same RLS GUC handling, same validation. Lives here because
the Dagster container doesn't mount the FastAPI ``app/`` tree, so a
direct import would fail at module-load.

Keep this in lockstep with the FastAPI module — the upsert SQL +
validation rules + ALLOWED sets MUST match. The two files exist purely
because there's no shared package between fastapi/ and dagster/ (a
proper fix is to extract a common library, but that's bigger than
this session). A drift detection regression test pins both sides.

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster's
asset_check decorator inspects runtime annotations.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger(__name__)


__all__ = [
    "DataQualityFlag",
    "upsert_flag_sync",
    "upsert_flags_sync",
    "ALLOWED_RECORD_TYPES",
    "ALLOWED_SEVERITIES",
]


# Mirror of the CHECK constraints on silver.data_quality_flags. The
# helper validates inputs BEFORE roundtrip so a typo surfaces as a
# clear ValueError instead of a CheckViolationError buried in a
# psycopg2 traceback.
ALLOWED_RECORD_TYPES = frozenset({
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

ALLOWED_SEVERITIES = frozenset({"INFO", "WARNING", "ERROR"})


@dataclass(frozen=True)
class DataQualityFlag:
    """Input shape for :func:`upsert_flag_sync`.

    Required fields are positional / kwarg; everything else is keyword-
    only + optional. Frozen so a batch list (passed to upsert_flags_sync)
    can't be mutated mid-iteration.
    """

    workspace_id: str
    record_type: str
    record_id: str
    flag_type: str
    severity: str
    description: str
    project_id: "str | None" = None
    source_document_id: "str | None" = None
    source_page: "int | None" = None
    source_row_range: "str | None" = None
    rule_id: "str | None" = None
    rule_version: "str | None" = None
    threshold_payload: "dict | None" = None


def _validate(flag: DataQualityFlag) -> None:
    """Argument-shape validation. Catches typos before the DB does."""
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


def _payload_jsonb(payload):
    """Serialise the threshold_payload dict for the JSONB column.

    None → '{}' so the DEFAULT column constraint stays satisfied.
    """
    if payload is None:
        return "{}"
    return json.dumps(payload)


# Same upsert SQL as the FastAPI sync variant. Keep these strings
# byte-identical — the idempotency key relies on the WHERE clause
# being literally the same.
_UPSERT_SQL = """
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


def upsert_flag_sync(conn, flag):
    """Idempotently insert or update one flag row.

    Args:
        conn: a psycopg2 connection (typically from
            ``PostgresResource.get_connection()``).
        flag: a :class:`DataQualityFlag` to write.

    Returns:
        True on success, False on DB-level error (logged at WARNING).
        Validation errors raise ValueError because they're caller bugs.
    """
    _validate(flag)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('georag.workspace_id', %s, true)",
                (flag.workspace_id,),
            )
            cur.execute(_UPSERT_SQL, {
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
            "dq_writer.upsert_failed workspace=%s record=%s/%s flag_type=%s",
            flag.workspace_id, flag.record_type, flag.record_id,
            flag.flag_type, exc_info=True,
        )
        return False


def upsert_flags_sync(conn, flags):
    """Batch variant — runs upsert_flag_sync per row.

    Caller is responsible for the final conn.commit(). Returns the
    number of successful writes; a single-flag failure logs + the
    batch keeps going.
    """
    if not flags:
        return 0
    written = 0
    for flag in flags:
        if upsert_flag_sync(conn, flag):
            written += 1
    return written
