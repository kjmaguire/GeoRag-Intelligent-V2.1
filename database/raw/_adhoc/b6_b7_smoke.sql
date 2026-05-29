-- Smoke test for B6 cross-section + B7 stereonet asset SQL.
-- Validates that the column names + JSONB shapes in the rewritten Dagster
-- assets actually match the live gold table schemas.

\set ON_ERROR_STOP on
\echo '--- B6 cross_section_panels UPSERT smoke ---'

INSERT INTO gold.cross_section_panels (
    panel_id, workspace_id, project_id, section_name,
    section_line_geom, azimuth_deg, length_m,
    collars_projected, x_extent_m, y_extent_m, buffer_m
)
VALUES (
    gen_random_uuid(),
    'a0000000-0000-0000-0000-000000000001'::uuid,
    '00000000-0000-0000-0000-000000000452'::uuid,
    '__smoke_b6__',
    ST_SetSRID(ST_MakeLine(ST_MakePoint(-106.5, 42.1), ST_MakePoint(-106.4, 42.15)), 4326),
    45.0, 7500.0,
    '[{"hole_id":"smoke","collar_id":"00000000-0000-0000-0000-000000000001","axis_distance_m":100,"perpendicular_offset_m":5,"collar_elevation_m":1800,"total_depth_m":120,"trace":[],"intervals":[]}]'::jsonb,
    1000.0, 200.0, 50.0
)
ON CONFLICT (project_id, section_name) DO UPDATE SET
    section_line_geom = EXCLUDED.section_line_geom,
    azimuth_deg       = EXCLUDED.azimuth_deg,
    length_m          = EXCLUDED.length_m,
    collars_projected = EXCLUDED.collars_projected,
    x_extent_m        = EXCLUDED.x_extent_m,
    y_extent_m        = EXCLUDED.y_extent_m,
    buffer_m          = EXCLUDED.buffer_m,
    computed_at       = now();

\echo '--- B6 read-back ---'
SELECT section_name, azimuth_deg, length_m,
       jsonb_array_length(collars_projected) AS nholes,
       ST_AsText(section_line_geom) AS geom
  FROM gold.cross_section_panels
 WHERE section_name = '__smoke_b6__';

\echo '--- B7 SELECT from silver.structure (proves table name + columns) ---'
SELECT COUNT(*)                                AS total_rows,
       COUNT(true_dip)                         AS with_true_dip,
       COUNT(true_dip_dir)                     AS with_true_dip_dir,
       COUNT(*) FILTER (WHERE true_dip IS NOT NULL
                           AND true_dip_dir IS NOT NULL) AS projectable
  FROM silver.structure;

\echo '--- B7 INSERT smoke into gold.structure_measurements_visual ---'
INSERT INTO gold.structure_measurements_visual (
    visual_id, collar_id, workspace_id, project_id,
    depth, structure_type, strike_deg, dip_deg, dip_direction_deg,
    plunge_deg, trend_deg, stereonet_x, stereonet_y, projection
)
SELECT
    gen_random_uuid(),
    c.collar_id,
    c.workspace_id,
    c.project_id,
    0.0,
    'fault',
    0.0, 45.0, 90.0,
    NULL, NULL,
    0.354, 0.0,
    'equal_area'
  FROM silver.collars c
 WHERE c.collar_id = '838582e4-4eee-4064-836f-a0dc7f6c2896';

\echo '--- B7 read-back ---'
SELECT structure_type, strike_deg, dip_deg, dip_direction_deg, stereonet_x, stereonet_y, projection
  FROM gold.structure_measurements_visual
 WHERE collar_id = '838582e4-4eee-4064-836f-a0dc7f6c2896'
 ORDER BY created_at DESC
 LIMIT 1;

\echo '--- cleanup ---'
DELETE FROM gold.cross_section_panels WHERE section_name = '__smoke_b6__';
DELETE FROM gold.structure_measurements_visual
 WHERE collar_id = '838582e4-4eee-4064-836f-a0dc7f6c2896'
   AND structure_type = 'fault' AND depth = 0.0;

\echo '--- DONE ---'
