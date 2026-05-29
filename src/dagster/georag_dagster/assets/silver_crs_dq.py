"""Plan §6a — CRS / georeferencing quality rule family.

Scans silver.collars + writes per-collar flags to silver.data_quality_flags
covering the four ways a collar's location can be untrustworthy:

  • collar.crs_assumed                  WARNING  georef_method='assumed'
  • collar.crs_low_confidence           WARNING  crs_confidence < 0.5
  • collar.geom_missing_with_coords     ERROR    geom IS NULL but easting/northing populated
  • collar.spatial_uncertainty_excessive WARNING spatial_uncertainty_m > 100m

Note the original plan was "CRS consistency: per-project SRID rollup" —
abandoned after schema inspection because silver.collars.geom is typed
`geometry(Point,32613)`. The column type enforces a single SRID at
this layer, so mixed-SRID-in-one-project simply can't happen here. The
real CRS bugs live one layer back, in the *provenance* columns
(georef_method, crs_confidence, spatial_uncertainty_m) that track HOW
the geom was derived. Those are per-collar properties, not project-
rollup, so each rule fires once per collar at most.

Per-collar evaluation mirrors silver_collar_dq (no fan-in needed —
each collar's georef quality is its own concern, not aggregated
across rows).

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13.

Sized 2026-05-29 at 2-3h focused implementation against the
silver_collar_dq template; landed in ~1.5h.
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


# Pinned across re-runs — bumping retires the prior rows for SME review.
# Don't change without a planned data migration; the upsert key relies
# on it staying stable.
RULE_VERSION = "v1.0"


# Threshold for the low-confidence rule. Picked at 0.5 because the live
# crs_confidence distribution sits between 0.3 and 0.7 — 0.5 splits the
# population cleanly into "trust this geom" vs "this came from an
# assumption that might be wrong".
CRS_CONFIDENCE_FLOOR = 0.5

# Threshold for the spatial-uncertainty rule, in metres. 100m is the
# typical resolution of a hand-marked map digitisation; anything worse
# than that means a geologist can't reliably tie a hole to a known
# structural feature without resampling. False-positive risk on
# legitimate exploration-stage holes is low — those are usually GPS-
# measured and report uncertainty < 10m.
SPATIAL_UNCERTAINTY_CEILING_M = 100.0


# ---------------------------------------------------------------------------
# SQL — fetch all collars + the georef-provenance columns.
# ---------------------------------------------------------------------------

SELECT_COLLARS_SQL = """
SELECT
    co.collar_id::text            AS collar_id,
    co.workspace_id::text         AS workspace_id,
    co.project_id::text           AS project_id,
    co.hole_id                    AS hole_id,
    co.easting                    AS easting,
    co.northing                   AS northing,
    co.geom IS NOT NULL           AS has_geom,
    co.georef_method              AS georef_method,
    co.crs_confidence             AS crs_confidence,
    co.spatial_uncertainty_m      AS spatial_uncertainty_m
FROM silver.collars co
"""


class CrsDQConfig(Config):
    """Asset config. Defaults to a full sweep; an operator can limit
    by workspace for an ad-hoc check during the rollout."""

    workspace_id: str | None = None
    """Optional workspace_id filter. None = sweep every workspace."""


# ---------------------------------------------------------------------------
# Pure rule evaluation — one row in, list[DataQualityFlag] out
# ---------------------------------------------------------------------------


def evaluate_collar_crs_row(row: dict) -> list[DataQualityFlag]:
    """Apply the 4 CRS / georef-quality rules to one row.

    Returns the list of flags this row should carry. Empty list means
    the row passes every rule. Pure function — no DB / no I/O — so
    unit-testable without a Dagster harness.
    """
    flags: list[DataQualityFlag] = []
    workspace_id = row["workspace_id"]
    project_id = row["project_id"]
    collar_id = row["collar_id"]
    hole_id = row.get("hole_id") or collar_id[:8]

    base = dict(
        workspace_id=workspace_id,
        project_id=project_id,
        record_type="collar",
        record_id=collar_id,
        rule_version=RULE_VERSION,
    )

    # Rule 1 — CRS was guessed, not declared. WARNING because the geom
    # might still be right (most assumptions hold) but the geologist
    # should at minimum spot-check it before relying on it for a
    # structural interpretation.
    if row.get("georef_method") == "assumed":
        flags.append(DataQualityFlag(
            **base,
            flag_type="collar.crs_assumed",
            severity="WARNING",
            description=(
                f"Collar {hole_id}: CRS was assumed by the ingest "
                f"pipeline (no source metadata declared the projection). "
                f"Spot-check the easting/northing against a known reference "
                f"point before relying on this location for cross-section "
                f"or structural work."
            ),
            rule_id="collar.crs_assumed",
            threshold_payload={
                "georef_method": "assumed",
                "crs_confidence": (
                    float(row["crs_confidence"])
                    if row.get("crs_confidence") is not None
                    else None
                ),
            },
        ))

    # Rule 2 — low-confidence CRS assignment. WARNING because the
    # confidence score is the ingest pipeline's own admission that the
    # CRS detection was shaky. Below 0.5 means the pipeline barely
    # ranked the chosen projection above the alternatives.
    conf = row.get("crs_confidence")
    if conf is not None and conf < CRS_CONFIDENCE_FLOOR:
        flags.append(DataQualityFlag(
            **base,
            flag_type="collar.crs_low_confidence",
            severity="WARNING",
            description=(
                f"Collar {hole_id}: CRS-detection confidence is "
                f"{conf:.2f} (below the {CRS_CONFIDENCE_FLOOR:.2f} "
                f"floor). The pipeline picked a projection but the score "
                f"was close to its alternatives. Verify the source "
                f"document declared the right CRS before using this collar "
                f"for compositing or cross-section."
            ),
            rule_id="collar.crs_low_confidence",
            threshold_payload={
                "observed": float(conf),
                "floor": CRS_CONFIDENCE_FLOOR,
            },
        ))

    # Rule 3 — geom resolution failed despite present coordinates.
    # ERROR because the collar can't render on any map, can't anchor
    # an intersection, and can't be queried by spatial filter. Almost
    # always means the declared CRS was un-resolvable (e.g. an obsolete
    # EPSG code) and the from-raw → silver step couldn't produce a
    # PostGIS geometry.
    east = row.get("easting")
    north = row.get("northing")
    has_geom = row.get("has_geom")
    if not has_geom and east is not None and north is not None:
        flags.append(DataQualityFlag(
            **base,
            flag_type="collar.geom_missing_with_coords",
            severity="ERROR",
            description=(
                f"Collar {hole_id}: easting={east}, northing={north} are "
                f"populated but geom is NULL. The CRS could not be "
                f"resolved into a PostGIS geometry — this collar is "
                f"invisible to every map view and spatial query. "
                f"Re-ingest with the correct CRS declared."
            ),
            rule_id="collar.geom_missing_with_coords",
            threshold_payload={
                "easting": float(east),
                "northing": float(north),
                "georef_method": row.get("georef_method"),
                "crs_confidence": (
                    float(row["crs_confidence"])
                    if row.get("crs_confidence") is not None
                    else None
                ),
            },
        ))

    # Rule 4 — recorded uncertainty too large to anchor structural
    # work. WARNING because the location is at least known approximately;
    # the geologist may still use it for regional context but not for
    # vein/structure correlation.
    unc = row.get("spatial_uncertainty_m")
    if unc is not None and unc > SPATIAL_UNCERTAINTY_CEILING_M:
        flags.append(DataQualityFlag(
            **base,
            flag_type="collar.spatial_uncertainty_excessive",
            severity="WARNING",
            description=(
                f"Collar {hole_id}: spatial_uncertainty_m={unc:g} m "
                f"exceeds the {SPATIAL_UNCERTAINTY_CEILING_M:g} m ceiling. "
                f"This collar is positionable for regional context but "
                f"NOT for structural correlation — don't tie veins or "
                f"fault traces to it without resampling the location."
            ),
            rule_id="collar.spatial_uncertainty_excessive",
            threshold_payload={
                "observed_m": float(unc),
                "ceiling_m": SPATIAL_UNCERTAINTY_CEILING_M,
            },
        ))

    return flags


# ---------------------------------------------------------------------------
# Asset — fetches rows + writes flags via the shared helper
# ---------------------------------------------------------------------------


@asset(
    group_name="data_quality",
    description=(
        "Plan §6a CRS / georeferencing-quality rule family. Scans "
        "silver.collars + writes flags to silver.data_quality_flags. "
        "Four rules: crs_assumed + crs_low_confidence + spatial_uncertainty "
        "(WARNING) + geom_missing_with_coords (ERROR). Per-collar, not "
        "rolled-up — each rule fires once per affected collar at most."
    ),
    compute_kind="postgres",
)
def silver_crs_dq(
    context: AssetExecutionContext,
    config: CrsDQConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Emit CRS / georef-quality flags for every silver.collars row."""
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
        "silver_crs_dq: scanning %d collar(s) (workspace_filter=%s)",
        len(rows), config.workspace_id or "all",
    )

    all_flags: list[DataQualityFlag] = []
    for row in rows:
        all_flags.extend(evaluate_collar_crs_row(row))

    # Per-rule rollup for the asset metadata. Useful in Dagit + lets
    # an SME see at a glance which rule is firing most.
    by_rule: dict[str, int] = {}
    severity_counts = {"INFO": 0, "WARNING": 0, "ERROR": 0}
    for f in all_flags:
        by_rule[f.flag_type] = by_rule.get(f.flag_type, 0) + 1
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
                    "silver_crs_dq: batch upsert failed — "
                    "no flags written this run"
                )
                raise

    context.log.info(
        "silver_crs_dq: wrote %d flag(s) across %d collar(s) — "
        "severities=%s, by_rule=%s",
        written, len(rows), severity_counts, by_rule,
    )

    return MaterializeResult(
        metadata={
            "collars_scanned": MetadataValue.int(len(rows)),
            "flags_written": MetadataValue.int(written),
            "flag_severity_error": MetadataValue.int(severity_counts["ERROR"]),
            "flag_severity_warning": MetadataValue.int(severity_counts["WARNING"]),
            "flag_severity_info": MetadataValue.int(severity_counts["INFO"]),
            "rule_version": MetadataValue.text(RULE_VERSION),
            "by_rule_json": MetadataValue.json(by_rule),
            "workspace_filter": MetadataValue.text(
                config.workspace_id or "(all workspaces)",
            ),
        },
    )
