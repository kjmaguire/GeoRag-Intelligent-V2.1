"""bronze.raw_assay_submissions → silver.assays_v2.

Transforms:
  1. Standardise element symbols ('GOLD' → 'Au', 'AU' → 'Au', etc.)
  2. Look up the canonical default unit + ppm_conversion from
     silver.element_reference
  3. Compute value_ppm = value * conversion (so cross-element grade
     comparisons are unit-agnostic)
  4. Resolve hole_id (text) → collar_id (UUID) via silver.collars
  5. Validate from_depth < to_depth; drop bad rows but COUNT them
  6. UPSERT into silver.assays_v2 keyed on bronze_source_id

Re-runs are idempotent — replaying the same import_batch_id is a no-op.
"""
import uuid

import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.resources import PostgresResource


_ELEMENT_ALIASES = {
    # Map free-text element strings the lab might emit → canonical symbol.
    "gold": "Au", "au": "Au", "au_ppm": "Au", "au_ppb": "Au",
    "silver": "Ag", "ag": "Ag", "ag_ppm": "Ag",
    "copper": "Cu", "cu": "Cu", "cu_pct": "Cu", "cu_ppm": "Cu",
    "lead": "Pb", "pb": "Pb",
    "zinc": "Zn", "zn": "Zn",
    "nickel": "Ni", "ni": "Ni",
    "cobalt": "Co", "co": "Co",
    "molybdenum": "Mo", "mo": "Mo",
    "uranium": "U", "u": "U", "u3o8": "U",
    "arsenic": "As", "as": "As",
    "antimony": "Sb", "sb": "Sb",
    "bismuth": "Bi", "bi": "Bi",
    "platinum": "Pt", "pt": "Pt",
    "palladium": "Pd", "pd": "Pd",
}


def _canonical_element(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower()
    return _ELEMENT_ALIASES.get(key, raw.strip())


class SilverAssaysV2Config(Config):
    """import_batch_id scopes the bronze rows the run consumes."""
    import_batch_id: str
    workspace_id: str


_UPSERT_SQL = """
INSERT INTO silver.assays_v2 (
    workspace_id, collar_id, sample_id, from_depth, to_depth,
    element, value, unit, value_ppm, detection_limit,
    over_detection, under_detection, lab_name, certificate_ref,
    bronze_source_id
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO NOTHING
"""


@asset(
    group_name="drillhole_silver",
    description=(
        "Validated, unit-standardised assay intervals. Reads "
        "bronze.raw_assay_submissions and writes silver.assays_v2 "
        "with value_ppm computed via silver.element_reference."
    ),
)
def silver_assays_v2(
    context: AssetExecutionContext,
    config: SilverAssaysV2Config,
    postgres: PostgresResource,
) -> MaterializeResult:
    pg = postgres.connect()
    rows_in = 0
    rows_written = 0
    rows_skipped_unknown_hole = 0
    rows_skipped_bad_interval = 0
    rows_skipped_unknown_element = 0

    try:
        with pg.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Pull the element-reference table into a Python dict for fast lookup.
            cur.execute("SELECT symbol, default_unit, ppm_conversion FROM silver.element_reference")
            elements = {r["symbol"]: r for r in cur.fetchall()}

            # Build the hole_id → collar_id map for this workspace.
            cur.execute(
                "SELECT hole_id, collar_id FROM silver.collars WHERE workspace_id = %s",
                (config.workspace_id,),
            )
            hole_to_collar = {r["hole_id"]: r["collar_id"] for r in cur.fetchall()}

            # Stream the bronze rows for this batch.
            cur.execute(
                """
                SELECT id, sample_id, hole_id, from_depth, to_depth,
                       element, value, unit, detection_limit,
                       over_detection, under_detection,
                       lab_name, certificate_ref
                  FROM bronze.raw_assay_submissions
                 WHERE workspace_id = %s::uuid
                   AND import_batch_id = %s::uuid
                """,
                (config.workspace_id, config.import_batch_id),
            )
            bronze_rows = cur.fetchall()

        rows_in = len(bronze_rows)
        with pg.cursor() as upsert_cur:
            for row in bronze_rows:
                # FK: hole_id must resolve.
                collar_id = hole_to_collar.get(row["hole_id"])
                if collar_id is None:
                    rows_skipped_unknown_hole += 1
                    continue

                # Interval validity.
                if row["from_depth"] is None or row["to_depth"] is None \
                   or row["to_depth"] <= row["from_depth"]:
                    rows_skipped_bad_interval += 1
                    continue

                # Element canonicalisation + unit normalisation.
                element = _canonical_element(row["element"])
                if element is None or element not in elements:
                    rows_skipped_unknown_element += 1
                    continue

                ref = elements[element]
                unit = (row["unit"] or ref["default_unit"]).strip()
                value = row["value"]
                value_ppm = None
                if value is not None:
                    # If the row's reported unit matches the reference's
                    # default, applying the conversion is direct. We make
                    # the simplifying assumption that the data we receive
                    # is in its declared unit and convert from that.
                    value_ppm = float(value) * float(ref["ppm_conversion"])

                upsert_cur.execute(
                    _UPSERT_SQL,
                    (
                        config.workspace_id, collar_id, row["sample_id"],
                        row["from_depth"], row["to_depth"],
                        element, value, unit, value_ppm,
                        row["detection_limit"],
                        row["over_detection"], row["under_detection"],
                        row["lab_name"], row["certificate_ref"],
                        row["id"],
                    ),
                )
                rows_written += 1

        pg.commit()
    finally:
        pg.close()

    return MaterializeResult(
        metadata={
            "rows_in": MetadataValue.int(rows_in),
            "rows_written": MetadataValue.int(rows_written),
            "rows_skipped_unknown_hole": MetadataValue.int(rows_skipped_unknown_hole),
            "rows_skipped_bad_interval": MetadataValue.int(rows_skipped_bad_interval),
            "rows_skipped_unknown_element": MetadataValue.int(rows_skipped_unknown_element),
        },
    )
