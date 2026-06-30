"""Silver-side derivation — α/β acoustic-televiewer angles → true_dip + true_dip_dir.

Reads ``silver.structure`` rows that have ``alpha_angle`` + ``beta_angle``
populated but NULL ``true_dip``/``true_dip_dir``, and back-fills the true
orientation using the standard core-to-world rotation.

Math (core coordinate frame, RIGHT-handed):
    Let α (alpha) be the angle from the core axis (0° = perpendicular to core,
    90° = parallel). Let β (beta) be the clockwise rotation around the core
    axis, measured from the top-of-hole reference line.

    Pole-to-plane unit vector in the core frame:
        n_x_c = sin(α) · cos(β)            # right (cross to top reference)
        n_y_c = sin(α) · sin(β)            # along top reference
        n_z_c = cos(α)                      # along core (down-hole positive)

    The core frame is rotated into the world frame using the local hole
    orientation at depth d:
        hole_az  = azimuth at depth d (clockwise from north, 0-360°)
        hole_dip = dip at depth d (0-90°, where 90° = vertical down)

    Rotation matrix R = R_z(hole_az) · R_y(90° − hole_dip):

        R = [[ cos(az)·sin(dip),  −sin(az), cos(az)·cos(dip) ],
             [ sin(az)·sin(dip),   cos(az), sin(az)·cos(dip) ],
             [-cos(dip),                  0, sin(dip)          ]]

    World-frame pole:
        n_world = R · n_core

    Then:
        true_dip      = arcsin(|n_world.z|)          # 0-90°
        true_dip_dir  = (atan2(n_world.y, n_world.x) → degrees, 0-360°)
                       pointing in the dip-down direction (n_world.z < 0).

For deviated holes the *local* hole_az/hole_dip at depth d must come from
``silver.drill_traces``. v1 of this asset uses the **collar** azimuth/dip as
the local orientation, which is exact for straight holes and good-to-a-few-
degrees for typical mineral exploration holes (collar→toe deviation usually
< 5° per 100 m). A future v2 should sample the LineStringZ geom at depth d.

Idempotent: only UPDATEs rows where ``true_dip IS NULL OR true_dip_dir IS
NULL`` AND both ``alpha_angle`` and ``beta_angle`` are present. Rows that
already have true orientations populated (e.g. via a vendor import) are
left alone.

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster 1.13.
"""

import math

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.resources import PostgresResource


SELECT_PENDING_SQL = """
SELECT
    s.id,
    s.collar_id,
    s.alpha_angle,
    s.beta_angle,
    c.azimuth,
    c.dip
FROM silver.structure s
JOIN silver.collars c ON c.collar_id = s.collar_id
WHERE c.workspace_id = %(workspace_id)s::uuid
  AND (%(project_id)s::uuid IS NULL OR c.project_id = %(project_id)s::uuid)
  AND s.alpha_angle IS NOT NULL
  AND s.beta_angle  IS NOT NULL
  AND (s.true_dip IS NULL OR s.true_dip_dir IS NULL)
  AND c.azimuth IS NOT NULL
  AND c.dip     IS NOT NULL
"""

UPDATE_SQL = """
UPDATE silver.structure
   SET true_dip     = %(true_dip)s,
       true_dip_dir = %(true_dip_dir)s
 WHERE id = %(id)s::uuid;
"""


def derive_true_orientation(
    alpha_deg: float, beta_deg: float,
    hole_az_deg: float, hole_dip_deg: float,
) -> tuple[float, float]:
    """Convert α/β + hole orientation to (true_dip, true_dip_dir) in degrees.

    Both inputs and outputs are in degrees. Returned dip is 0-90°, dip-dir
    is 0-360°. Caller is responsible for unit conversion if needed.
    """
    a = math.radians(alpha_deg)
    b = math.radians(beta_deg)
    az = math.radians(hole_az_deg)
    di = math.radians(hole_dip_deg)

    # Pole vector in core frame.
    n_x_c = math.sin(a) * math.cos(b)
    n_y_c = math.sin(a) * math.sin(b)
    n_z_c = math.cos(a)

    # Rotation: core-z (down-hole) maps to (cos(az)·cos(dip), sin(az)·cos(dip), -sin(dip))?
    # Convention: hole_dip is positive degrees BELOW horizontal. For a vertical
    # hole hole_dip=90° → core-z maps to (-0, 0, -1) world (straight down).
    # We use the rotation R = R_z(az) · R_y(90° - dip).
    # Equivalent expanded matrix application:
    cos_az, sin_az = math.cos(az), math.sin(az)
    cos_di, sin_di = math.cos(di), math.sin(di)

    # Rotated pole.
    n_world_x =  cos_az * sin_di * n_x_c + (-sin_az) * n_y_c + cos_az * cos_di * n_z_c
    n_world_y =  sin_az * sin_di * n_x_c +   cos_az  * n_y_c + sin_az * cos_di * n_z_c
    n_world_z = -cos_di          * n_x_c +     0      * n_y_c + sin_di          * n_z_c

    # The plane's normal can point up or down; the dip direction is the
    # azimuth of the down-pointing projection in the horizontal plane.
    if n_world_z > 0:
        # Flip so n points downward (lower-hemisphere convention).
        n_world_x, n_world_y, n_world_z = -n_world_x, -n_world_y, -n_world_z

    horiz_mag = math.hypot(n_world_x, n_world_y)
    # true_dip = angle between the plane and horizontal = angle between the
    # pole and vertical = atan2(horizontal component, vertical component).
    true_dip = math.degrees(math.atan2(horiz_mag, abs(n_world_z)))
    # Clamp to [0, 90].
    true_dip = max(0.0, min(90.0, true_dip))

    if horiz_mag < 1e-9:
        # Horizontal plane — dip direction is undefined; convention = 0°.
        true_dip_dir = 0.0
    else:
        # Dip direction = azimuth (0=N, clockwise) of the dip-down direction.
        # atan2(x, y) gives az from north going clockwise.
        true_dip_dir = math.degrees(math.atan2(n_world_x, n_world_y))
        true_dip_dir = (true_dip_dir + 360.0) % 360.0

    return true_dip, true_dip_dir


class SilverStructureDeriveConfig(Config):
    """Runtime configuration for the silver_structure_derive asset."""

    workspace_id: str
    project_id: str = ""


@asset(
    group_name="silver",
    description=(
        "Back-fill silver.structure.true_dip + true_dip_dir from alpha_angle "
        "/ beta_angle (acoustic-televiewer convention) via the standard "
        "core-to-world rotation. Uses collar azimuth/dip as the local hole "
        "orientation (good approximation for straight holes; v2 will sample "
        "silver.drill_traces at depth)."
    ),
)
def silver_structure_derive(
    context: AssetExecutionContext,
    config: SilverStructureDeriveConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Derive true_dip + true_dip_dir for α/β-only rows."""

    project_id_val = config.project_id if config.project_id else None

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                SELECT_PENDING_SQL,
                {"workspace_id": config.workspace_id, "project_id": project_id_val},
            )
            pending = cur.fetchall()

        with conn.cursor() as cur:
            updates = []
            for row in pending:
                td, tdd = derive_true_orientation(
                    float(row["alpha_angle"]),
                    float(row["beta_angle"]),
                    float(row["azimuth"]),
                    float(row["dip"]),
                )
                updates.append({
                    "id":           str(row["id"]),
                    "true_dip":     round(td, 3),
                    "true_dip_dir": round(tdd, 3),
                })
            if updates:
                psycopg2.extras.execute_batch(cur, UPDATE_SQL, updates, page_size=200)
        conn.commit()

    context.log.info(
        "silver_structure_derive: workspace=%s candidates=%d updated=%d",
        config.workspace_id, len(pending), len(updates),
    )

    return MaterializeResult(
        metadata={
            "workspace_id":  MetadataValue.text(config.workspace_id),
            "project_id":    MetadataValue.text(project_id_val or ""),
            "candidate_rows": MetadataValue.int(len(pending)),
            "updated_rows":   MetadataValue.int(len(updates)),
        }
    )
