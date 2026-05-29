"""Plan §6a — Collar validation rule family.

Scans silver.collars and writes flag rows to silver.data_quality_flags
for the data-quality issues a geologist needs to see on the document
view badge. Six rules emitted, ranked by severity:

  • collar.invalid_dip_range          ERROR    dip > 0 OR dip < -90
  • collar.invalid_azimuth_range      ERROR    azimuth < 0 OR azimuth >= 360
  • collar.invalid_total_depth        ERROR    total_depth NULL or <= 0
  • collar.missing_elevation          WARNING  elevation IS NULL
  • collar.missing_azimuth            INFO     azimuth IS NULL
  • collar.missing_dip                INFO     dip IS NULL

Idempotent via silver_dq_flag_writer.upsert_flag_sync — re-running the
asset is a no-op on stable rows; the upsert UPDATEs flag rows when the
underlying data changes (e.g. elevation backfilled → the
missing_elevation flag's resolution lifecycle resets and an SME can
mark it 'corrected').

Workspace + project tenancy: every flag carries the workspace_id +
project_id from the collar row. RLS GUC is set per-row by the writer
helper.

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13.

Sized 2026-05-29 at 2-3h focused implementation; landed in ~1.5h.
"""

import logging

from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.dq_writer import DataQualityFlag, upsert_flags_sync
from georag_dagster.resources import PostgresResource


logger = logging.getLogger(__name__)


# Rule definitions are versioned. Bumping the rule_version retires the
# old flag rows for that rule (they become "stale" until an SME
# reviews; new flags fire under the new version). Don't change the
# rule_id once published — that's the discriminator the badge UI
# clusters on.
RULE_VERSION = "v1.0"


# ---------------------------------------------------------------------------
# SQL — fetch all collars + denormalised join to silver.reports so we
# can stamp source_document_id on each flag (drives the document-view
# badge lookup).
# ---------------------------------------------------------------------------

SELECT_COLLARS_SQL = """
SELECT
    co.collar_id::text                AS collar_id,
    co.workspace_id::text             AS workspace_id,
    co.project_id::text               AS project_id,
    co.hole_id                        AS hole_id,
    co.elevation                      AS elevation,
    co.azimuth                        AS azimuth,
    co.dip                            AS dip,
    co.total_depth                    AS total_depth,
    -- bronze_source_id is the ingest-manifest ref; rendering join via
    -- silver.reports happens at the UI badge query, not here.
    co.bronze_source_id::text         AS bronze_source_id
FROM silver.collars co
"""


class CollarDQConfig(Config):
    """Asset config. Defaults to a full sweep; an operator can limit
    by workspace for an ad-hoc check during the rollout."""

    workspace_id: str | None = None
    """Optional workspace_id filter. None = sweep every workspace."""


# ---------------------------------------------------------------------------
# Pure rule evaluation — one row in, list[DataQualityFlag] out
# ---------------------------------------------------------------------------


def evaluate_collar_row(row: dict) -> list[DataQualityFlag]:
    """Apply the 6 collar rules to one row.

    Returns the list of flags this row should carry. Empty list means
    the row passes every rule. Pure function — no DB / no I/O — so
    unit-testable without a Dagster harness.
    """
    flags: list[DataQualityFlag] = []
    workspace_id = row["workspace_id"]
    project_id = row["project_id"]
    collar_id = row["collar_id"]
    hole_id = row.get("hole_id") or "(unknown)"

    base = dict(
        workspace_id=workspace_id,
        project_id=project_id,
        record_type="collar",
        record_id=collar_id,
        rule_version=RULE_VERSION,
    )

    # Rule 1 — dip range. Dip MUST be in [-90, 0] (positive dip means
    # the hole is going UP, physically impossible for a drilled hole).
    dip = row.get("dip")
    if dip is not None and (dip > 0 or dip < -90):
        flags.append(DataQualityFlag(
            **base,
            flag_type="collar.invalid_dip_range",
            severity="ERROR",
            description=(
                f"Collar {hole_id}: dip={dip}° is outside the valid "
                f"range [-90°, 0°]. Positive dip indicates an upward "
                f"hole (physically impossible); below -90° is over-vertical."
            ),
            rule_id="collar.invalid_dip_range",
            threshold_payload={"min": -90, "max": 0, "observed": dip},
        ))

    # Rule 2 — azimuth range. Standard convention is [0, 360).
    az = row.get("azimuth")
    if az is not None and (az < 0 or az >= 360):
        flags.append(DataQualityFlag(
            **base,
            flag_type="collar.invalid_azimuth_range",
            severity="ERROR",
            description=(
                f"Collar {hole_id}: azimuth={az}° is outside the valid "
                f"range [0°, 360°). Re-check the source data — a "
                f"negative or ≥360 value is usually a transcription bug."
            ),
            rule_id="collar.invalid_azimuth_range",
            threshold_payload={"min": 0, "max_exclusive": 360, "observed": az},
        ))

    # Rule 3 — total_depth must be positive (NULL or <= 0 is invalid).
    td = row.get("total_depth")
    if td is None or td <= 0:
        flags.append(DataQualityFlag(
            **base,
            flag_type="collar.invalid_total_depth",
            severity="ERROR",
            description=(
                f"Collar {hole_id}: total_depth={td} is invalid "
                f"(must be > 0)."
            ),
            rule_id="collar.invalid_total_depth",
            threshold_payload={"observed": td},
        ))

    # Rule 4 — missing elevation. WARNING (not ERROR) because some
    # legacy ingests omit it; SMEs can backfill from the source PDF.
    if row.get("elevation") is None:
        flags.append(DataQualityFlag(
            **base,
            flag_type="collar.missing_elevation",
            severity="WARNING",
            description=(
                f"Collar {hole_id}: elevation is NULL. Back-calc from the "
                f"source PDF or DEM lookup; assays referenced to this "
                f"collar can't be tied to true vertical depth without it."
            ),
            rule_id="collar.missing_elevation",
        ))

    # Rules 5 & 6 — missing orientation. INFO because vertical holes
    # often record neither (azimuth is meaningless on a vertical, and
    # dip=-90 may have been ingested as NULL).
    if row.get("azimuth") is None:
        flags.append(DataQualityFlag(
            **base,
            flag_type="collar.missing_azimuth",
            severity="INFO",
            description=(
                f"Collar {hole_id}: azimuth is NULL. Vertical holes "
                f"don't need it; angled holes do. Verify with the "
                f"original drill log."
            ),
            rule_id="collar.missing_azimuth",
        ))

    if row.get("dip") is None:
        flags.append(DataQualityFlag(
            **base,
            flag_type="collar.missing_dip",
            severity="INFO",
            description=(
                f"Collar {hole_id}: dip is NULL. Verify with the original "
                f"drill log; -90° (vertical) is the most common implied "
                f"default but should be explicit."
            ),
            rule_id="collar.missing_dip",
        ))

    return flags


# ---------------------------------------------------------------------------
# Asset — fetches rows + writes flags via the shared helper
# ---------------------------------------------------------------------------


@asset(
    group_name="data_quality",
    description=(
        "Plan §6a collar validation rule family. Scans silver.collars + "
        "writes flag rows to silver.data_quality_flags. Six rules: "
        "invalid dip/azimuth/total_depth (ERROR) + missing elevation "
        "(WARNING) + missing azimuth/dip (INFO)."
    ),
    compute_kind="postgres",
)
def silver_collar_dq(
    context: AssetExecutionContext,
    config: CollarDQConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Emit data-quality flags for every silver.collars row."""
    sql = SELECT_COLLARS_SQL
    params: tuple = ()
    if config.workspace_id:
        sql += " WHERE co.workspace_id = %s::uuid"
        params = (config.workspace_id,)

    with postgres.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    context.log.info(
        "silver_collar_dq: scanning %d collar(s) (workspace_filter=%s)",
        len(rows), config.workspace_id or "all",
    )

    all_flags: list[DataQualityFlag] = []
    for row in rows:
        all_flags.extend(evaluate_collar_row(row))

    # Per-severity rollup for the asset metadata. Useful in Dagit + lets
    # an SME see "are we producing more INFO than ERROR" without
    # querying the table directly.
    severity_counts = {"INFO": 0, "WARNING": 0, "ERROR": 0}
    for f in all_flags:
        severity_counts[f.severity] += 1

    written = 0
    if all_flags:
        with postgres.get_connection() as conn:
            try:
                written = upsert_flags_sync(conn, all_flags)
                conn.commit()
            except Exception:
                conn.rollback()
                context.log.exception(
                    "silver_collar_dq: batch upsert failed — "
                    "no flags written this run"
                )
                raise

    context.log.info(
        "silver_collar_dq: wrote %d flag(s) across %d collar(s) — "
        "severities=%s",
        written, len(rows), severity_counts,
    )

    return MaterializeResult(
        metadata={
            "collars_scanned": MetadataValue.int(len(rows)),
            "flags_written": MetadataValue.int(written),
            "flag_severity_error": MetadataValue.int(severity_counts["ERROR"]),
            "flag_severity_warning": MetadataValue.int(severity_counts["WARNING"]),
            "flag_severity_info": MetadataValue.int(severity_counts["INFO"]),
            "rule_version": MetadataValue.text(RULE_VERSION),
            "workspace_filter": MetadataValue.text(
                config.workspace_id or "(all workspaces)",
            ),
        },
    )
