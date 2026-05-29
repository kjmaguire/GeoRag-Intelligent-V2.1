-- =============================================================================
-- GeoRAG pgTAP — Golden MVT Snapshot Seed Fixture
-- File: database/tests/pgtap/seed_golden_fixture.sql
-- Module 8 Chunk 8.8 — Deliverable B
-- =============================================================================
--
-- Idempotent seed: inserts deterministic test rows for all 7 silver MVT
-- function source tables under a fixed "GoldenFixture" project.
--
-- Project  id : 00000000-0000-0000-0000-deadbeefcafe
-- Workspace id: a0000000-0000-0000-0000-000000000001  (default workspace)
-- data_version: 1 (set once; monotonic trigger forbids decrement)
-- crs_epsg    : 32613  (UTM zone 13N)
-- Tile tested : z=3, x=1, y=2  (covers lon -135…-90, lat ~41…67)
-- Center area : lon≈-110, lat≈55  (easting≈500000, northing≈6100000 in 32613)
--
-- Run:
--   docker compose exec postgresql psql -U georag -d georag \
--     -f /pgtap/seed_golden_fixture.sql
--
-- Teardown (used by generate.sh after snapshot capture):
--   At bottom of this file as a labelled block; or run manually.
-- =============================================================================

-- ── 0. Workspace (already exists in prod; ON CONFLICT is a no-op) ─────────────
INSERT INTO silver.workspaces (workspace_id, name, slug, data_version)
VALUES (
    'a0000000-0000-0000-0000-000000000001',
    'Default Workspace',
    'default-workspace',
    0
)
ON CONFLICT (workspace_id) DO NOTHING;

-- ── 1. Project ────────────────────────────────────────────────────────────────
-- data_version starts at 0 (column default); we UPDATE to 1 after INSERT
-- because the monotonic trigger only blocks decrements, not increments.
INSERT INTO silver.projects (
    project_id,
    project_name,
    slug,
    workspace_id,
    status,
    crs_epsg,
    crs_datum,
    orientation_reference,
    data_version
)
VALUES (
    '00000000-0000-0000-0000-deadbeefcafe',
    'GoldenFixture',
    'golden-fixture',
    'a0000000-0000-0000-0000-000000000001',
    'active',
    32613,
    'EPSG:32613',
    'true_north',
    1
)
ON CONFLICT (project_id) DO NOTHING;

-- ── 2. Collars (3 rows, EPSG:32613 Points inside tile 3/1/2) ─────────────────
-- Collar geometry: EPSG:32613 Points
-- lon=-110, lat=55  →  easting≈500000, northing≈6100000
-- lon=-112, lat=55  →  easting≈328000, northing≈6100000
-- lon=-108, lat=55  →  easting≈670000, northing≈6100000
-- (all well within tile 3/1/2 which spans lon -135…-90)
INSERT INTO silver.collars (
    collar_id,
    hole_id,
    project_id,
    easting,
    northing,
    elevation,
    total_depth,
    hole_type,
    azimuth,
    dip,
    status,
    geom
)
VALUES
(
    'b0000001-0000-0000-0000-deadbeefcafe',
    'GF-001',
    '00000000-0000-0000-0000-deadbeefcafe',
    500000.0,
    6100000.0,
    500.0,
    250.0,
    'DDH',
    45.0,
    -60.0,
    'completed',
    ST_SetSRID(ST_MakePoint(500000.0, 6100000.0), 32613)
),
(
    'b0000002-0000-0000-0000-deadbeefcafe',
    'GF-002',
    '00000000-0000-0000-0000-deadbeefcafe',
    501500.0,
    6100500.0,
    502.0,
    180.0,
    'DDH',
    90.0,
    -55.0,
    'completed',
    ST_SetSRID(ST_MakePoint(501500.0, 6100500.0), 32613)
),
(
    'b0000003-0000-0000-0000-deadbeefcafe',
    'GF-003',
    '00000000-0000-0000-0000-deadbeefcafe',
    502500.0,
    6101000.0,
    498.0,
    320.0,
    'DDH',
    180.0,
    -70.0,
    'completed',
    ST_SetSRID(ST_MakePoint(502500.0, 6101000.0), 32613)
)
ON CONFLICT (project_id, hole_id) DO NOTHING;

-- ── 3. Drill traces (1 LineStringZ per collar, EPSG:4326) ─────────────────────
-- Traces must span enough degrees to survive ST_SimplifyPreserveTopology(100m)
-- at z=3 tile level. Use ~5° longitude span (≫ 10-20km at lat=55).
-- survey_hash must be exactly 64 hex chars.
INSERT INTO silver.drill_traces (
    trace_id,
    collar_id,
    workspace_id,
    project_id,
    geom,
    survey_hash,
    dogleg_max_deg,
    trace_quality
)
VALUES
(
    'c0000001-0000-0000-0000-deadbeefcafe',
    'b0000001-0000-0000-0000-deadbeefcafe',
    'a0000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-deadbeefcafe',
    ST_GeomFromText(
        'LINESTRING Z (-110.0 55.0 500.0, -115.0 57.0 300.0)',
        4326
    ),
    'aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899',
    1.2,
    'ok'
),
(
    'c0000002-0000-0000-0000-deadbeefcafe',
    'b0000002-0000-0000-0000-deadbeefcafe',
    'a0000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-deadbeefcafe',
    ST_GeomFromText(
        'LINESTRING Z (-110.5 55.5 502.0, -114.5 57.5 300.0)',
        4326
    ),
    'bbccddeeff00112233445566778899aabbccddeeff00112233445566778899aa',
    0.8,
    'ok'
),
(
    'c0000003-0000-0000-0000-deadbeefcafe',
    'b0000003-0000-0000-0000-deadbeefcafe',
    'a0000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-deadbeefcafe',
    ST_GeomFromText(
        'LINESTRING Z (-109.0 55.2 498.0, -114.0 57.2 250.0)',
        4326
    ),
    'ccddeeff00112233445566778899aabbccddeeff00112233445566778899aabb',
    2.1,
    'high_dogleg_warning'
)
ON CONFLICT (collar_id) DO NOTHING;

-- ── 4. Project boundary (1 MultiPolygon, EPSG:4326) ──────────────────────────
INSERT INTO silver.project_boundaries (
    id,
    workspace_id,
    project_id,
    boundary_name,
    boundary_type,
    geom
)
VALUES (
    'd0000001-0000-0000-0000-deadbeefcafe',
    'a0000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-deadbeefcafe',
    'GoldenFixture Claim Block',
    'claim',
    ST_GeomFromText(
        'MULTIPOLYGON (((-112.0 54.0, -108.0 54.0, -108.0 57.0, -112.0 57.0, -112.0 54.0)))',
        4326
    )
)
ON CONFLICT (id) DO NOTHING;

-- ── 5. Geological formation (1 MultiPolygon, EPSG:4326) ──────────────────────
INSERT INTO silver.geological_formations (
    id,
    workspace_id,
    project_id,
    formation_code,
    formation_name,
    age_period,
    age_ma_lower,
    age_ma_upper,
    lithology_primary,
    geom
)
VALUES (
    'e0000001-0000-0000-0000-deadbeefcafe',
    'a0000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-deadbeefcafe',
    'GF-GRAN',
    'Golden Granite',
    'Precambrian',
    2500.0,
    2700.0,
    'granite',
    ST_GeomFromText(
        'MULTIPOLYGON (((-111.5 54.5, -109.5 54.5, -109.5 56.5, -111.5 56.5, -111.5 54.5)))',
        4326
    )
)
ON CONFLICT (project_id, formation_code) DO NOTHING;

-- ── 6. Historic workings (2 Points, EPSG:4326) ───────────────────────────────
INSERT INTO silver.historic_workings (
    id,
    workspace_id,
    project_id,
    working_name,
    working_type,
    operational_period,
    operational_from_year,
    operational_to_year,
    commodity_codes,
    status,
    geom
)
VALUES
(
    'f0000001-0000-0000-0000-deadbeefcafe',
    'a0000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-deadbeefcafe',
    'Golden Adit No. 1',
    'adit',
    '1920-1935',
    1920,
    1935,
    ARRAY['Au', 'Ag'],
    'abandoned',
    ST_GeomFromText('POINT (-110.5 55.3)', 4326)
),
(
    'f0000002-0000-0000-0000-deadbeefcafe',
    'a0000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-deadbeefcafe',
    'Golden Shaft No. 2',
    'shaft',
    '1940-1960',
    1940,
    1960,
    ARRAY['Au'],
    'abandoned',
    ST_GeomFromText('POINT (-110.8 55.7)', 4326)
)
ON CONFLICT (id) DO NOTHING;

-- ── 7. Seismic survey (1 bbox Polygon, EPSG:4326) ────────────────────────────
INSERT INTO silver.seismic_surveys (
    survey_id,
    project_id,
    survey_name,
    survey_type,
    num_traces,
    num_samples_per_trace,
    sample_interval_us,
    record_length_ms,
    source_file,
    file_size_bytes,
    bbox
)
VALUES (
    'a0000001-0000-0000-0000-deadbeefcafe',
    '00000000-0000-0000-0000-deadbeefcafe',
    'GoldenFixture 3D Survey',
    '3D',
    5000,
    1000,
    2000,
    2000.0,
    'golden_fixture_3d.segy',
    104857600,
    ST_GeomFromText(
        'POLYGON ((-111.0 54.8, -109.5 54.8, -109.5 56.0, -111.0 56.0, -111.0 54.8))',
        4326
    )
)
ON CONFLICT (survey_id) DO NOTHING;

-- ── 8. Geochemistry (3 rows, EPSG:4326 Points, project_id required) ──────────
-- collar_id FK references the collars inserted above.
-- geom: ST_Transform(collar.geom_32613, 4326) equivalent points.
-- geochem_id must be deterministic for snapshot reproducibility.
INSERT INTO silver.geochemistry (
    geochem_id,
    collar_id,
    from_depth,
    to_depth,
    sio2_wt_pct,
    al2o3_wt_pct,
    fe2o3_wt_pct,
    project_id,
    workspace_id,
    geom,
    sample_id,
    sample_type,
    assay_element_codes,
    assay_values_ppm
)
VALUES
(
    'a1000001-0000-0000-0000-deadbeefcafe',
    'b0000001-0000-0000-0000-deadbeefcafe',
    0.0,
    2.0,
    68.5,
    15.2,
    4.1,
    '00000000-0000-0000-0000-deadbeefcafe',
    'a0000000-0000-0000-0000-000000000001',
    ST_GeomFromText('POINT (-110.0 55.0)', 4326),
    'GF-GC-001',
    'rock_chip',
    ARRAY['Si', 'Al', 'Fe'],
    '{"Au_ppb": 125, "Ag_ppm": 2.3}'::jsonb
),
(
    'a1000002-0000-0000-0000-deadbeefcafe',
    'b0000002-0000-0000-0000-deadbeefcafe',
    0.0,
    2.0,
    71.0,
    13.8,
    3.5,
    '00000000-0000-0000-0000-deadbeefcafe',
    'a0000000-0000-0000-0000-000000000001',
    ST_GeomFromText('POINT (-110.5 55.5)', 4326),
    'GF-GC-002',
    'rock_chip',
    ARRAY['Si', 'Al', 'Fe'],
    '{"Au_ppb": 87, "Ag_ppm": 1.8}'::jsonb
),
(
    'a1000003-0000-0000-0000-deadbeefcafe',
    'b0000003-0000-0000-0000-deadbeefcafe',
    0.0,
    2.0,
    65.0,
    17.1,
    5.2,
    '00000000-0000-0000-0000-deadbeefcafe',
    'a0000000-0000-0000-0000-000000000001',
    ST_GeomFromText('POINT (-109.0 55.2)', 4326),
    'GF-GC-003',
    'rock_chip',
    ARRAY['Si', 'Al', 'Fe'],
    '{"Au_ppb": 210, "Ag_ppm": 3.1}'::jsonb
)
ON CONFLICT (geochem_id) DO NOTHING;

-- ── Verify insertion counts ───────────────────────────────────────────────────
DO $$
DECLARE
    v_pid uuid := '00000000-0000-0000-0000-deadbeefcafe';
BEGIN
    ASSERT (SELECT COUNT(*) FROM silver.collars WHERE project_id = v_pid) >= 3,
        'Expected at least 3 collar rows for GoldenFixture project';
    ASSERT (SELECT COUNT(*) FROM silver.drill_traces WHERE project_id = v_pid) >= 3,
        'Expected at least 3 drill trace rows for GoldenFixture project';
    ASSERT (SELECT COUNT(*) FROM silver.project_boundaries WHERE project_id = v_pid) >= 1,
        'Expected at least 1 boundary row for GoldenFixture project';
    ASSERT (SELECT COUNT(*) FROM silver.geological_formations WHERE project_id = v_pid) >= 1,
        'Expected at least 1 formation row for GoldenFixture project';
    ASSERT (SELECT COUNT(*) FROM silver.historic_workings WHERE project_id = v_pid) >= 2,
        'Expected at least 2 historic workings rows for GoldenFixture project';
    ASSERT (SELECT COUNT(*) FROM silver.seismic_surveys WHERE project_id = v_pid) >= 1,
        'Expected at least 1 seismic survey row for GoldenFixture project';
    ASSERT (SELECT COUNT(*) FROM silver.geochemistry WHERE project_id = v_pid) >= 3,
        'Expected at least 3 geochemistry rows for GoldenFixture project';
    RAISE NOTICE 'GoldenFixture seed: all 7 table counts verified OK.';
END;
$$;

-- =============================================================================
-- TEARDOWN (for generate.sh post-capture cleanup; NOT run automatically)
-- To clean: psql -U georag -d georag -c "\i seed_golden_fixture_teardown.sql"
-- Or run the block below manually:
--
-- DO $$
-- BEGIN
--     DELETE FROM silver.geochemistry  WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe';
--     DELETE FROM silver.seismic_surveys WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe';
--     DELETE FROM silver.historic_workings WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe';
--     DELETE FROM silver.geological_formations WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe';
--     DELETE FROM silver.project_boundaries WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe';
--     DELETE FROM silver.drill_traces WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe';
--     DELETE FROM silver.collars WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe';
--     DELETE FROM silver.projects WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe';
--     RAISE NOTICE 'GoldenFixture teardown complete.';
-- END;
-- $$;
-- =============================================================================
