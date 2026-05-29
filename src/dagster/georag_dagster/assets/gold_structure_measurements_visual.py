"""Gold layer asset — stereonet-ready structure measurements.

§5 Phase H4 Step 5.8. Reads ``silver.structure`` (singular — confirmed via
\\dt on live PG 2026-05-22) and projects each measurement onto a unit-disk
stereonet, storing the result in ``gold.structure_measurements_visual``.

silver.structure schema (real columns):
  id              uuid PK
  workspace_id    uuid
  collar_id       uuid FK silver.collars
  depth           numeric
  structure_type  text
  alpha_angle     numeric  (acoustic-televiewer α — angle from core axis)
  beta_angle      numeric  (acoustic-televiewer β — rotation around core axis)
  true_dip        numeric  (already-converted planar dip 0-90°)
  true_dip_dir    numeric  (planar dip direction 0-360°)
  roughness       text
  infill          text
  notes           text

When the row has ``true_dip`` + ``true_dip_dir`` populated, we use those
directly as the planar measurement. Otherwise we fall back to converting
α/β + collar (azimuth, dip) to true orientations — v1 punts on that and
skips α/β-only rows with a warning (the project's existing acQuire imports
populate true_dip/true_dip_dir, so this is rarely the operative branch).

Equal-area (Schmidt) projection (lower-hemisphere) for planar pole-to-plane:

    pole_plunge = 90° - true_dip
    pole_azimuth = (true_dip_dir + 180°) mod 360°
    ρ = √2 · sin((90° - pole_plunge) / 2)
    x = ρ · sin(pole_azimuth)
    y = ρ · cos(pole_azimuth)

Idempotent: DELETE-by-workspace + INSERT keeps the table aligned with the
upstream silver.structure snapshot.

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13.
"""

import math
import uuid
from typing import Optional

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.resources import PostgresResource


SELECT_STRUCTURES_SQL = """
SELECT
    s.collar_id,
    c.workspace_id,
    c.project_id,
    s.depth,
    s.structure_type,
    s.alpha_angle,
    s.beta_angle,
    s.true_dip,
    s.true_dip_dir
FROM silver.structure s
JOIN silver.collars c ON c.collar_id = s.collar_id
WHERE c.workspace_id = %(workspace_id)s::uuid
  AND (%(project_id)s::uuid IS NULL OR c.project_id = %(project_id)s::uuid)
"""

DELETE_EXISTING_SQL = """
DELETE FROM gold.structure_measurements_visual
WHERE workspace_id = %(workspace_id)s::uuid
  AND (%(project_id)s::uuid IS NULL OR project_id = %(project_id)s::uuid);
"""

INSERT_SQL = """
INSERT INTO gold.structure_measurements_visual (
    visual_id, collar_id, workspace_id, project_id,
    depth, structure_type, strike_deg, dip_deg, dip_direction_deg,
    plunge_deg, trend_deg,
    stereonet_x, stereonet_y, projection
) VALUES (
    %(visual_id)s, %(collar_id)s, %(workspace_id)s, %(project_id)s,
    %(depth)s, %(structure_type)s, %(strike_deg)s, %(dip_deg)s, %(dip_direction_deg)s,
    %(plunge_deg)s, %(trend_deg)s,
    %(stereonet_x)s, %(stereonet_y)s, %(projection)s
);
"""

# silver.structure has no CHECK on structure_type; gold table CHECKs against
# this 12-value vocabulary. Map silver values into it, falling back to 'other'.
VALID_STRUCTURE_TYPES = frozenset({
    "fault", "shear", "fracture", "joint", "vein", "foliation", "cleavage",
    "bedding", "contact", "fold_axis", "lineation", "other",
})


def _project_planar_pole(
    true_dip_deg: float, true_dip_dir_deg: float, projection: str,
) -> tuple[float, float]:
    """Pole-to-plane stereonet projection (lower hemisphere)."""
    pole_plunge = 90.0 - true_dip_deg
    pole_azimuth = (true_dip_dir_deg + 180.0) % 360.0

    plunge_rad = math.radians(pole_plunge)
    azimuth_rad = math.radians(pole_azimuth)

    if projection == "equal_angle":
        rho = math.tan((math.pi / 2.0 - plunge_rad) / 2.0)
    else:
        rho = math.sqrt(2.0) * math.sin((math.pi / 2.0 - plunge_rad) / 2.0)

    x = rho * math.sin(azimuth_rad)
    y = rho * math.cos(azimuth_rad)
    return x, y


def _classify_structure_type(raw: Optional[str]) -> str:
    if not raw:
        return "other"
    norm = raw.strip().lower()
    return norm if norm in VALID_STRUCTURE_TYPES else "other"


class GoldStereonetConfig(Config):
    workspace_id: str
    project_id: str = ""
    projection: str = "equal_area"


@asset(
    group_name="gold",
    description=(
        "Read silver.structure + silver.collars, project each planar "
        "measurement onto a unit-disk stereonet (equal-area by default), "
        "and replace the workspace's rows in gold.structure_measurements_visual."
    ),
)
def gold_structure_measurements_visual(
    context: AssetExecutionContext,
    config: GoldStereonetConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Project silver.structure onto a stereonet and store the result."""

    if config.projection not in ("equal_area", "equal_angle"):
        raise ValueError(
            f"projection must be 'equal_area' or 'equal_angle' (got '{config.projection}')"
        )

    project_id_val = config.project_id if config.project_id else None

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                SELECT_STRUCTURES_SQL,
                {"workspace_id": config.workspace_id, "project_id": project_id_val},
            )
            structures = cur.fetchall()

        with conn.cursor() as cur:
            cur.execute(
                DELETE_EXISTING_SQL,
                {"workspace_id": config.workspace_id, "project_id": project_id_val},
            )
            deleted = cur.rowcount

            insert_params = []
            skipped_no_orientation = 0
            for s in structures:
                # v1: rely on true_dip + true_dip_dir. α/β-only rows skip with a
                # warning until the AT-survey conversion lands as a silver-side
                # derive (Phase 7 candidate).
                if s["true_dip"] is None or s["true_dip_dir"] is None:
                    skipped_no_orientation += 1
                    continue

                true_dip = float(s["true_dip"])
                true_dip_dir = float(s["true_dip_dir"])
                x, y = _project_planar_pole(true_dip, true_dip_dir, config.projection)
                # Strike = dip_direction - 90° (right-hand rule, 0-360 normalised).
                strike = (true_dip_dir - 90.0) % 360.0

                insert_params.append({
                    "visual_id":         str(uuid.uuid4()),
                    "collar_id":         str(s["collar_id"]),
                    "workspace_id":      str(s["workspace_id"]),
                    "project_id":        str(s["project_id"]) if s["project_id"] else None,
                    "depth":             s["depth"],
                    "structure_type":    _classify_structure_type(s["structure_type"]),
                    "strike_deg":        strike,
                    "dip_deg":           true_dip,
                    "dip_direction_deg": true_dip_dir,
                    "plunge_deg":        None,
                    "trend_deg":         None,
                    "stereonet_x":       x,
                    "stereonet_y":       y,
                    "projection":        config.projection,
                })

            inserted = 0
            if insert_params:
                psycopg2.extras.execute_batch(
                    cur, INSERT_SQL, insert_params, page_size=200
                )
                inserted = len(insert_params)
        conn.commit()

    if skipped_no_orientation:
        context.log.warning(
            "gold_structure_measurements_visual: skipped %d α/β-only rows "
            "(true_dip / true_dip_dir NULL). Needs silver-side derivation.",
            skipped_no_orientation,
        )

    context.log.info(
        "gold_structure_measurements_visual: workspace=%s deleted=%d inserted=%d skipped=%d",
        config.workspace_id, deleted, inserted, skipped_no_orientation,
    )

    return MaterializeResult(
        metadata={
            "workspace_id":          MetadataValue.text(config.workspace_id),
            "project_id":            MetadataValue.text(project_id_val or ""),
            "projection":            MetadataValue.text(config.projection),
            "structures_read":       MetadataValue.int(len(structures)),
            "deleted_before_load":   MetadataValue.int(deleted),
            "inserted":              MetadataValue.int(inserted),
            "skipped_no_orientation": MetadataValue.int(skipped_no_orientation),
        }
    )
