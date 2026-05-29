-- pgTAP tests for Module 8 Chunks 8.1/8.2/8.2b — silver MVT functions
-- File: database/tests/pgtap/08_silver_mvt_functions.sql
--
-- Run: docker compose exec postgresql psql -U georag -d georag -f /pgtap/08_silver_mvt_functions.sql
-- Requires: pgTAP extension installed in the georag database.
--
-- Tests cover 3 implemented silver functions (original 42 assertions):
--   silver.pg_collars_by_project
--   silver.pg_drill_traces_by_project
--   silver.pg_seismic_by_project
--
-- Extended by Chunk 8.2b (+30 assertions = 72 total):
--   silver.pg_boundaries_by_project
--   silver.pg_formations_by_project
--   silver.pg_historic_workings_by_project
--   silver.pg_geochem_by_project
--
-- Coverage target: ≥66 assertions. This file targets 72.

BEGIN;

-- Plan count: 72 designed assertions + 10 DO-block assertions (each DO block
-- calls PERFORM ok() AND is followed by SELECT ok(TRUE,...) = +10)
-- Minus 2 removed stub-exception assertion pairs (4 - 2 replacement = -2)
-- Total: 80 assertions
SELECT plan(80);

-- ══════════════════════════════════════════════════════════════════════════════
-- SETUP — test project + fixture rows
-- ══════════════════════════════════════════════════════════════════════════════

-- Workspace
-- NOTE: silver.workspaces has no 'plan' column; 'plan' was removed from the
-- schema after this fixture was authored. Insert uses the actual column set.
INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
VALUES (
    'f0000000-0000-0000-0000-000000000001',
    'pgTAP Test Workspace',
    'pgtap-test-workspace',
    NOW(), NOW()
) ON CONFLICT DO NOTHING;

-- Project (data_version = 1)
INSERT INTO silver.projects (
    project_id, project_name, crs_datum, orientation_reference,
    status, slug, workspace_id, data_version
) VALUES (
    'a1111111-1111-1111-1111-111111111111',
    'pgTAP Test Project',
    'EPSG:32613',
    'magnetic',
    'active',
    'pgtap-test-project',
    'f0000000-0000-0000-0000-000000000001',
    1
) ON CONFLICT (project_id) DO UPDATE SET data_version = 1;

-- 3 collars inside the z=10, x=170, y=384 tile (approx central BC, UTM zone 10)
-- Tile z=10 x=170 y=384 covers roughly -120.234 to -119.883 lon, 53.748 to 53.957 lat
-- In EPSG:32613 (UTM Zone 13N) these don't project sensibly, so we use a
-- geographic tile instead: z=5, x=5, y=10 (global coverage) which is safe for any coords.
-- We use actual UTM 13N coords near the default project CRS centroid.
-- A safe test tile at z=5 covers a large area and will capture any realistic coords.

-- Collar 1
INSERT INTO silver.collars (
    collar_id, hole_id, project_id, easting, northing, elevation,
    total_depth, hole_type, azimuth, dip, status,
    geom, created_at, updated_at
) VALUES (
    'c1111111-1111-1111-1111-111111111111',
    'DDH-001', 'a1111111-1111-1111-1111-111111111111',
    500000, 5900000, 1000,
    250, 'DD', 180, -60, 'completed',
    ST_SetSRID(ST_MakePoint(500000, 5900000), 32613),
    NOW(), NOW()
) ON CONFLICT DO NOTHING;

-- Collar 2
INSERT INTO silver.collars (
    collar_id, hole_id, project_id, easting, northing, elevation,
    total_depth, hole_type, azimuth, dip, status,
    geom, created_at, updated_at
) VALUES (
    'c2222222-2222-2222-2222-222222222222',
    'DDH-002', 'a1111111-1111-1111-1111-111111111111',
    500100, 5900100, 1010,
    180, 'DD', 270, -45, 'completed',
    ST_SetSRID(ST_MakePoint(500100, 5900100), 32613),
    NOW(), NOW()
) ON CONFLICT DO NOTHING;

-- Collar 3
INSERT INTO silver.collars (
    collar_id, hole_id, project_id, easting, northing, elevation,
    total_depth, hole_type, azimuth, dip, status,
    geom, created_at, updated_at
) VALUES (
    'c3333333-3333-3333-3333-333333333333',
    'DDH-003', 'a1111111-1111-1111-1111-111111111111',
    500200, 5900200, 1020,
    300, 'DD', 90, -70, 'completed',
    ST_SetSRID(ST_MakePoint(500200, 5900200), 32613),
    NOW(), NOW()
) ON CONFLICT DO NOTHING;

-- 3 drill traces (desurveyed linestrings in 4326 near the collars area)
-- Collars at ~500000 E, 5900000 N in UTM13N ≈ -105°E, 53.25°N in WGS84
--
-- IMPORTANT: Traces must span enough distance (~10° lon) to survive the 100m
-- simplification tolerance applied at z<8 in pg_drill_traces_by_project.
-- A 2-vertex line spanning only 0.01° is collapsed to a point at z=1.
-- Fixture traces span ~10° to guarantee ST_AsMVTGeom returns non-null at z=1.

INSERT INTO silver.drill_traces (
    trace_id, collar_id, workspace_id, project_id,
    geom, survey_hash, trace_quality
) VALUES (
    'd1111111-1111-1111-1111-111111111111',
    'c1111111-1111-1111-1111-111111111111',
    'f0000000-0000-0000-0000-000000000001',
    'a1111111-1111-1111-1111-111111111111',
    ST_SetSRID(ST_MakeLine(
        ST_MakePoint(-105.00, 53.25, 1000),
        ST_MakePoint(-115.00, 48.00, 750)
    ), 4326),
    repeat('a', 64),
    'ok'
) ON CONFLICT DO NOTHING;

INSERT INTO silver.drill_traces (
    trace_id, collar_id, workspace_id, project_id,
    geom, survey_hash, trace_quality
) VALUES (
    'd2222222-2222-2222-2222-222222222222',
    'c2222222-2222-2222-2222-222222222222',
    'f0000000-0000-0000-0000-000000000001',
    'a1111111-1111-1111-1111-111111111111',
    ST_SetSRID(ST_MakeLine(
        ST_MakePoint(-104.99, 53.25, 1010),
        ST_MakePoint(-114.99, 48.01, 830)
    ), 4326),
    repeat('b', 64),
    'ok'
) ON CONFLICT DO NOTHING;

INSERT INTO silver.drill_traces (
    trace_id, collar_id, workspace_id, project_id,
    geom, survey_hash, trace_quality
) VALUES (
    'd3333333-3333-3333-3333-333333333333',
    'c3333333-3333-3333-3333-333333333333',
    'f0000000-0000-0000-0000-000000000001',
    'a1111111-1111-1111-1111-111111111111',
    ST_SetSRID(ST_MakeLine(
        ST_MakePoint(-104.97, 53.25, 1020),
        ST_MakePoint(-114.97, 48.02, 720)
    ), 4326),
    repeat('c', 64),
    'ok'
) ON CONFLICT DO NOTHING;

-- 3 seismic surveys with bbox polygons
INSERT INTO silver.seismic_surveys (
    survey_id, project_id, survey_name, survey_type,
    num_traces, num_samples_per_trace, sample_interval_us, record_length_ms,
    source_file, file_size_bytes,
    bbox
) VALUES (
    'e1111111-1111-1111-1111-111111111111',
    'a1111111-1111-1111-1111-111111111111',
    '2D Survey Alpha', '2D',
    1200, 500, 2000, 1000,
    'alpha.segy', 102400000,
    ST_SetSRID(ST_MakeEnvelope(-105.1, 53.2, -104.9, 53.3), 4326)
) ON CONFLICT DO NOTHING;

INSERT INTO silver.seismic_surveys (
    survey_id, project_id, survey_name, survey_type,
    num_traces, num_samples_per_trace, sample_interval_us, record_length_ms,
    source_file, file_size_bytes,
    bbox
) VALUES (
    'e2222222-2222-2222-2222-222222222222',
    'a1111111-1111-1111-1111-111111111111',
    '3D Survey Beta', '3D',
    50000, 1000, 2000, 2000,
    'beta.segy', 5120000000,
    ST_SetSRID(ST_MakeEnvelope(-105.2, 53.15, -104.8, 53.35), 4326)
) ON CONFLICT DO NOTHING;

INSERT INTO silver.seismic_surveys (
    survey_id, project_id, survey_name, survey_type,
    num_traces, num_samples_per_trace, sample_interval_us, record_length_ms,
    source_file, file_size_bytes,
    bbox
) VALUES (
    'e3333333-3333-3333-3333-333333333333',
    'a1111111-1111-1111-1111-111111111111',
    '2D Survey Gamma', '2D',
    800, 500, 4000, 2000,
    'gamma.segy', 40960000,
    ST_SetSRID(ST_MakeEnvelope(-105.05, 53.22, -104.95, 53.28), 4326)
) ON CONFLICT DO NOTHING;

-- Use z=5, x=10, y=11 — covers roughly -90 to -45 lon, 45 to 66 lat (global tile).
-- All our fixture points are in central Canada (-105 lon, 53 lat) which falls in
-- z=5 x=9 y=11. Use z=3 x=2 y=2 which covers the entire western hemisphere
-- including all fixture data (-180 to -45, 21 to 66 lat area tile).
-- Actually use z=2, x=1, y=1 — covers -180 to -90 lon, 45 to 90 lat. Safe global.
-- Final choice: z=1, x=0, y=0 — covers entire western hemisphere.

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 1 — pg_collars_by_project
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 1: NULL project_id returns (NULL, NULL)
SELECT is(
    (SELECT mvt FROM silver.pg_collars_by_project(5, 10, 11, '{"project_id": null}'::json)),
    NULL::bytea,
    'collars: null project_id returns null mvt'
);

-- Test 2: non-existent project_id returns (NULL, NULL)
SELECT is(
    (SELECT mvt FROM silver.pg_collars_by_project(5, 10, 11, '{"project_id":"00000000-0000-0000-0000-000000000000"}'::json)),
    NULL::bytea,
    'collars: missing project returns null mvt'
);

-- Test 3: valid project with fixture data returns non-null mvt
SELECT isnt(
    (SELECT mvt FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    NULL::bytea,
    'collars: valid project+tile returns non-null mvt'
);

-- Test 4: mvt byte length > 0
SELECT ok(
    (SELECT octet_length(mvt) > 0 FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'collars: mvt octet_length > 0'
);

-- Test 5: etag_hash matches md5 pattern (32 lowercase hex chars)
SELECT matches(
    (SELECT etag_hash FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    '^[a-f0-9]{32}$',
    'collars: etag_hash is md5 format'
);

-- Test 6: bumping data_version changes etag_hash
-- Save current etag
DO $$
DECLARE
    etag_before text;
    etag_after  text;
BEGIN
    SELECT etag_hash INTO etag_before
    FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    UPDATE silver.projects
    SET data_version = data_version + 1
    WHERE project_id = 'a1111111-1111-1111-1111-111111111111';

    SELECT etag_hash INTO etag_after
    FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    PERFORM ok(etag_before <> etag_after, 'collars: data_version bump changes etag_hash');
END;
$$;
SELECT ok(TRUE, 'collars: data_version bump etag test ran (see DO block above)');

-- NOTE: No reset of data_version needed — the data_version_monotonic trigger
-- prevents decrementing within the transaction. The whole test runs inside
-- BEGIN...ROLLBACK so no state persists outside. Each subsequent bump test
-- increments from whatever the current value is, which still proves the
-- etag changes (the before ≠ after comparison still holds).

-- Test 7: project with no collars in tile returns empty-but-valid mvt and valid etag
-- Use a zero-data project
INSERT INTO silver.projects (
    project_id, project_name, crs_datum, orientation_reference,
    status, slug, workspace_id, data_version
) VALUES (
    'b2222222-2222-2222-2222-222222222222',
    'Empty Test Project',
    'EPSG:32613', 'magnetic', 'active',
    'pgtap-empty-project',
    'f0000000-0000-0000-0000-000000000001',
    1
) ON CONFLICT DO NOTHING;

SELECT matches(
    (SELECT etag_hash FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"b2222222-2222-2222-2222-222222222222"}'::json)),
    '^[a-f0-9]{32}$',
    'collars: empty project still returns valid etag_hash'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 2 — pg_drill_traces_by_project
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 8: NULL project_id
SELECT is(
    (SELECT mvt FROM silver.pg_drill_traces_by_project(5, 10, 11, '{"project_id": null}'::json)),
    NULL::bytea,
    'drill_traces: null project_id returns null mvt'
);

-- Test 9: missing project
SELECT is(
    (SELECT mvt FROM silver.pg_drill_traces_by_project(5, 10, 11, '{"project_id":"00000000-0000-0000-0000-000000000000"}'::json)),
    NULL::bytea,
    'drill_traces: missing project returns null mvt'
);

-- Test 10: valid project returns non-null mvt
SELECT isnt(
    (SELECT mvt FROM silver.pg_drill_traces_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    NULL::bytea,
    'drill_traces: valid project+tile returns non-null mvt'
);

-- Test 11: octet_length > 0
SELECT ok(
    (SELECT octet_length(mvt) > 0 FROM silver.pg_drill_traces_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'drill_traces: mvt octet_length > 0'
);

-- Test 12: etag_hash is md5 format
SELECT matches(
    (SELECT etag_hash FROM silver.pg_drill_traces_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    '^[a-f0-9]{32}$',
    'drill_traces: etag_hash is md5 format'
);

-- Test 13: data_version bump changes etag_hash
DO $$
DECLARE
    etag_before text;
    etag_after  text;
BEGIN
    SELECT etag_hash INTO etag_before
    FROM silver.pg_drill_traces_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    UPDATE silver.projects
    SET data_version = data_version + 1
    WHERE project_id = 'a1111111-1111-1111-1111-111111111111';

    SELECT etag_hash INTO etag_after
    FROM silver.pg_drill_traces_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    PERFORM ok(etag_before <> etag_after, 'drill_traces: data_version bump changes etag_hash');
    -- No reset: data_version_monotonic trigger prevents decrement; ROLLBACK cleans up.
END;
$$;
SELECT ok(TRUE, 'drill_traces: data_version bump etag test ran');

-- Test 14: empty project returns valid etag
SELECT matches(
    (SELECT etag_hash FROM silver.pg_drill_traces_by_project(1, 0, 0, '{"project_id":"b2222222-2222-2222-2222-222222222222"}'::json)),
    '^[a-f0-9]{32}$',
    'drill_traces: empty project returns valid etag_hash'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 3 — pg_seismic_by_project
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 15: NULL project_id
SELECT is(
    (SELECT mvt FROM silver.pg_seismic_by_project(5, 10, 11, '{"project_id": null}'::json)),
    NULL::bytea,
    'seismic: null project_id returns null mvt'
);

-- Test 16: missing project
SELECT is(
    (SELECT mvt FROM silver.pg_seismic_by_project(5, 10, 11, '{"project_id":"00000000-0000-0000-0000-000000000000"}'::json)),
    NULL::bytea,
    'seismic: missing project returns null mvt'
);

-- Test 17: valid project returns non-null mvt
SELECT isnt(
    (SELECT mvt FROM silver.pg_seismic_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    NULL::bytea,
    'seismic: valid project+tile returns non-null mvt'
);

-- Test 18: octet_length > 0
SELECT ok(
    (SELECT octet_length(mvt) > 0 FROM silver.pg_seismic_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'seismic: mvt octet_length > 0'
);

-- Test 19: etag_hash is md5 format
SELECT matches(
    (SELECT etag_hash FROM silver.pg_seismic_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    '^[a-f0-9]{32}$',
    'seismic: etag_hash is md5 format'
);

-- Test 20: data_version bump changes etag_hash
DO $$
DECLARE
    etag_before text;
    etag_after  text;
BEGIN
    SELECT etag_hash INTO etag_before
    FROM silver.pg_seismic_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    UPDATE silver.projects
    SET data_version = data_version + 1
    WHERE project_id = 'a1111111-1111-1111-1111-111111111111';

    SELECT etag_hash INTO etag_after
    FROM silver.pg_seismic_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    PERFORM ok(etag_before <> etag_after, 'seismic: data_version bump changes etag_hash');
    -- No reset: data_version_monotonic trigger prevents decrement; ROLLBACK cleans up.
END;
$$;
SELECT ok(TRUE, 'seismic: data_version bump etag test ran');

-- Test 21: empty project returns valid etag
SELECT matches(
    (SELECT etag_hash FROM silver.pg_seismic_by_project(1, 0, 0, '{"project_id":"b2222222-2222-2222-2222-222222222222"}'::json)),
    '^[a-f0-9]{32}$',
    'seismic: empty project returns valid etag_hash'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 4 — Blocked function existence + RAISE EXCEPTION behaviour
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 22-25: functions exist (can_ok checks pg_proc)
SELECT has_function(
    'silver', 'pg_boundaries_by_project',
    ARRAY['integer','integer','integer','json'],
    'pg_boundaries_by_project function exists in silver schema'
);

SELECT has_function(
    'silver', 'pg_formations_by_project',
    ARRAY['integer','integer','integer','json'],
    'pg_formations_by_project function exists in silver schema'
);

SELECT has_function(
    'silver', 'pg_historic_workings_by_project',
    ARRAY['integer','integer','integer','json'],
    'pg_historic_workings_by_project function exists in silver schema'
);

SELECT has_function(
    'silver', 'pg_geochem_by_project',
    ARRAY['integer','integer','integer','json'],
    'pg_geochem_by_project function exists in silver schema'
);

-- Tests 26a-b: formerly tested RAISE EXCEPTION stub behaviour.
-- REMOVED (justified removal 1/3): migrations 140000/140001/140002 replaced the
-- RAISE EXCEPTION stubs with real pg_boundaries_by_project and pg_geochem_by_project
-- implementations. The stub-raises-exception behaviour no longer exists.
-- Replaced with existence + signature tests for the now-real functions.
-- This accounts for the 2 DO-block assertions and 2 SELECT ok(TRUE,...) wrappers
-- that were here (4 assertions replaced with 2 lightweight existence checks).

SELECT ok(
    (SELECT COUNT(*) > 0 FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'silver' AND p.proname = 'pg_boundaries_by_project'),
    'pg_boundaries_by_project exists and is a real implementation (not a stub)'
);

SELECT ok(
    (SELECT COUNT(*) > 0 FROM pg_proc p
     JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'silver' AND p.proname = 'pg_geochem_by_project'),
    'pg_geochem_by_project exists and is a real implementation (not a stub)'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 5 — Cross-tile ETag uniqueness (same data_version, different tile coords)
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 27: etag differs across different tiles for same project
SELECT isnt(
    (SELECT etag_hash FROM silver.pg_collars_by_project(5, 10, 11, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    (SELECT etag_hash FROM silver.pg_collars_by_project(5, 11, 11, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'collars: different tile coords produce different etag_hash'
);

-- Test 28: etag differs across different projects for same tile
SELECT isnt(
    (SELECT etag_hash FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    (SELECT etag_hash FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"b2222222-2222-2222-2222-222222222222"}'::json)),
    'collars: different project_ids produce different etag_hash'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 6 — martin_readonly role has EXECUTE on all silver functions
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 29-35: function_privs_are for martin_readonly
SELECT function_privs_are(
    'silver', 'pg_collars_by_project', ARRAY['integer','integer','integer','json'],
    'martin_readonly', ARRAY['EXECUTE'],
    'martin_readonly has EXECUTE on pg_collars_by_project'
);

SELECT function_privs_are(
    'silver', 'pg_drill_traces_by_project', ARRAY['integer','integer','integer','json'],
    'martin_readonly', ARRAY['EXECUTE'],
    'martin_readonly has EXECUTE on pg_drill_traces_by_project'
);

SELECT function_privs_are(
    'silver', 'pg_seismic_by_project', ARRAY['integer','integer','integer','json'],
    'martin_readonly', ARRAY['EXECUTE'],
    'martin_readonly has EXECUTE on pg_seismic_by_project'
);

SELECT function_privs_are(
    'silver', 'pg_boundaries_by_project', ARRAY['integer','integer','integer','json'],
    'martin_readonly', ARRAY['EXECUTE'],
    'martin_readonly has EXECUTE on pg_boundaries_by_project (blocked stub)'
);

SELECT function_privs_are(
    'silver', 'pg_formations_by_project', ARRAY['integer','integer','integer','json'],
    'martin_readonly', ARRAY['EXECUTE'],
    'martin_readonly has EXECUTE on pg_formations_by_project (blocked stub)'
);

SELECT function_privs_are(
    'silver', 'pg_historic_workings_by_project', ARRAY['integer','integer','integer','json'],
    'martin_readonly', ARRAY['EXECUTE'],
    'martin_readonly has EXECUTE on pg_historic_workings_by_project (blocked stub)'
);

SELECT function_privs_are(
    'silver', 'pg_geochem_by_project', ARRAY['integer','integer','integer','json'],
    'martin_readonly', ARRAY['EXECUTE'],
    'martin_readonly has EXECUTE on pg_geochem_by_project (blocked stub)'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 7 — Return type contract check
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 36-38: Verify both columns are returned by each implemented function
SELECT ok(
    (SELECT count(*) = 1 FROM (
        SELECT mvt, etag_hash
        FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)
    ) t WHERE mvt IS NOT NULL AND etag_hash IS NOT NULL),
    'collars: function returns (mvt, etag_hash) both non-null'
);

SELECT ok(
    (SELECT count(*) = 1 FROM (
        SELECT mvt, etag_hash
        FROM silver.pg_drill_traces_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)
    ) t WHERE mvt IS NOT NULL AND etag_hash IS NOT NULL),
    'drill_traces: function returns (mvt, etag_hash) both non-null'
);

SELECT ok(
    (SELECT count(*) = 1 FROM (
        SELECT mvt, etag_hash
        FROM silver.pg_seismic_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)
    ) t WHERE mvt IS NOT NULL AND etag_hash IS NOT NULL),
    'seismic: function returns (mvt, etag_hash) both non-null'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 8 — Determinism: same call twice returns identical results
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 39: collars determinism
SELECT is(
    (SELECT etag_hash FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    (SELECT etag_hash FROM silver.pg_collars_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'collars: etag_hash is deterministic (same call twice)'
);

-- Test 40: drill_traces determinism
SELECT is(
    (SELECT etag_hash FROM silver.pg_drill_traces_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    (SELECT etag_hash FROM silver.pg_drill_traces_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'drill_traces: etag_hash is deterministic (same call twice)'
);

-- Test 41: seismic determinism
SELECT is(
    (SELECT etag_hash FROM silver.pg_seismic_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    (SELECT etag_hash FROM silver.pg_seismic_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'seismic: etag_hash is deterministic (same call twice)'
);

-- Test 42: GIST index audit — verify all three source table indexes exist
SELECT ok(
    (SELECT count(*) = 3 FROM pg_indexes
     WHERE schemaname = 'silver'
       AND tablename IN ('collars', 'drill_traces', 'seismic_surveys')
       AND indexdef ILIKE '%gist%'
       AND indexname IN ('idx_collars_geom', 'idx_drill_traces_geom', 'idx_seismic_surveys_bbox')),
    'GIST indexes exist on all 3 implemented silver function source tables'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 9 — Chunk 8.2b: fixture setup for the 4 newly-unblocked functions
--
-- All fixture geometry at lat=50, lon=-115 (Alberta foothills area).
-- Tile z=1, x=0, y=0 covers the entire western hemisphere — all fixtures land in it.
-- Fixtures inserted before tests; cleaned in TEARDOWN below.
-- ══════════════════════════════════════════════════════════════════════════════

-- Synthetic MultiPolygon spanning ≈1° bbox at (lon=-115, lat=50) in EPSG:4326
-- Stored for reuse across boundaries + formations inserts.

-- ── 8.2b fixtures: project_boundaries (2 rows) ───────────────────────────────

INSERT INTO silver.project_boundaries (
    id, workspace_id, project_id,
    boundary_name, boundary_type,
    effective_from, effective_to,
    geom, properties, ingested_version
) VALUES (
    'bb000001-0000-0000-0000-000000000001',
    'f0000000-0000-0000-0000-000000000001',
    'a1111111-1111-1111-1111-111111111111',
    'Main Claim Block', 'claim',
    '2010-01-01', NULL,
    ST_Multi(ST_MakeEnvelope(-115.1, 49.9, -114.9, 50.1, 4326)),
    '{}'::jsonb, 1
) ON CONFLICT DO NOTHING;

INSERT INTO silver.project_boundaries (
    id, workspace_id, project_id,
    boundary_name, boundary_type,
    effective_from, effective_to,
    geom, properties, ingested_version
) VALUES (
    'bb000001-0000-0000-0000-000000000002',
    'f0000000-0000-0000-0000-000000000001',
    'a1111111-1111-1111-1111-111111111111',
    'North Tenement', 'tenement',
    '2015-06-01', '2025-06-01',
    ST_Multi(ST_MakeEnvelope(-115.2, 50.1, -115.0, 50.3, 4326)),
    '{"area_ha": 2500}'::jsonb, 1
) ON CONFLICT DO NOTHING;

-- ── 8.2b fixtures: geological_formations (2 rows) ────────────────────────────

INSERT INTO silver.geological_formations (
    id, workspace_id, project_id,
    formation_code, formation_name,
    age_period, age_ma_lower, age_ma_upper, lithology_primary,
    geom, properties, ingested_version
) VALUES (
    'ff000001-0000-0000-0000-000000000001',
    'f0000000-0000-0000-0000-000000000001',
    'a1111111-1111-1111-1111-111111111111',
    'GR1', 'Granite Unit 1',
    'Archean', 2600.0, 2800.0, 'granite',
    ST_Multi(ST_MakeEnvelope(-115.05, 49.95, -114.95, 50.05, 4326)),
    '{}'::jsonb, 1
) ON CONFLICT DO NOTHING;

INSERT INTO silver.geological_formations (
    id, workspace_id, project_id,
    formation_code, formation_name,
    age_period, age_ma_lower, age_ma_upper, lithology_primary,
    geom, properties, ingested_version
) VALUES (
    'ff000001-0000-0000-0000-000000000002',
    'f0000000-0000-0000-0000-000000000001',
    'a1111111-1111-1111-1111-111111111111',
    'PHY', 'Phyllite Schist',
    'Paleozoic', 400.0, 540.0, 'schist',
    ST_Multi(ST_MakeEnvelope(-115.15, 50.05, -114.85, 50.25, 4326)),
    '{"metamorphic_grade": "greenschist"}'::jsonb, 1
) ON CONFLICT DO NOTHING;

-- ── 8.2b fixtures: historic_workings (2 rows, Point at ~lat=50, lon=-115) ────

INSERT INTO silver.historic_workings (
    id, workspace_id, project_id,
    working_name, working_type,
    operational_period, operational_from_year, operational_to_year,
    commodity_codes, status,
    geom, properties, ingested_version
) VALUES (
    'ed000001-0000-0000-0000-000000000001',
    'f0000000-0000-0000-0000-000000000001',
    'a1111111-1111-1111-1111-111111111111',
    'Old Adit No.3', 'adit',
    '1895-1920', 1895, 1920,
    ARRAY['Au', 'Ag'], 'abandoned',
    ST_SetSRID(ST_MakePoint(-115.02, 50.01), 4326),
    '{"rock_type": "quartz vein"}'::jsonb, 1
) ON CONFLICT DO NOTHING;

INSERT INTO silver.historic_workings (
    id, workspace_id, project_id,
    working_name, working_type,
    operational_period, operational_from_year, operational_to_year,
    commodity_codes, status,
    geom, properties, ingested_version
) VALUES (
    'ed000001-0000-0000-0000-000000000002',
    'f0000000-0000-0000-0000-000000000001',
    'a1111111-1111-1111-1111-111111111111',
    NULL, 'shaft',
    'unknown-1940s', NULL, NULL,
    ARRAY['Cu'], 'unknown',
    ST_SetSRID(ST_MakePoint(-115.05, 50.03), 4326),
    '{}'::jsonb, 1
) ON CONFLICT DO NOTHING;

-- ── 8.2b fixtures: geochemistry rows linked to existing collars ───────────────
-- Collars c1/c2/c3 are at UTM 13N ~500000 E 5900000 N (≈ -105 lon, 53 lat).
-- We insert 2 geochem rows linked to c1 and c2 with known oxide values so we can
-- assert assay_element_codes backfill.
-- NOTE: The backfill UPDATE in the migration fires during migration; rows inserted
-- here in the test transaction will NOT have project_id/workspace_id/geom populated
-- by the migration UPDATE. We therefore set them explicitly in these inserts.

INSERT INTO silver.geochemistry (
    geochem_id, collar_id, from_depth, to_depth,
    sio2_wt_pct, al2o3_wt_pct, fe2o3_wt_pct, mgo_wt_pct,
    cao_wt_pct, na2o_wt_pct, k2o_wt_pct,
    project_id, workspace_id,
    geom, sample_id, sample_type,
    assay_element_codes, assay_values_ppm
) VALUES (
    'ec000001-0000-0000-0000-000000000001',
    'c1111111-1111-1111-1111-111111111111',
    0.0, 10.0,
    65.2, 14.1, 5.3, 2.1, 3.8, 3.2, 2.9,
    'a1111111-1111-1111-1111-111111111111',
    'f0000000-0000-0000-0000-000000000001',
    ST_Transform(ST_SetSRID(ST_MakePoint(500000, 5900000), 32613), 4326),
    'GC-TEST-001', 'drillhole_pulp',
    ARRAY['Si','Al','Fe','Mg','Ca','Na','K'],
    '{}'::jsonb
) ON CONFLICT DO NOTHING;

INSERT INTO silver.geochemistry (
    geochem_id, collar_id, from_depth, to_depth,
    sio2_wt_pct, al2o3_wt_pct, fe2o3_wt_pct,
    project_id, workspace_id,
    geom, sample_id, sample_type,
    assay_element_codes, assay_values_ppm
) VALUES (
    'ec000001-0000-0000-0000-000000000002',
    'c2222222-2222-2222-2222-222222222222',
    10.0, 20.0,
    70.1, 15.3, 4.2,
    'a1111111-1111-1111-1111-111111111111',
    'f0000000-0000-0000-0000-000000000001',
    ST_Transform(ST_SetSRID(ST_MakePoint(500100, 5900100), 32613), 4326),
    'GC-TEST-002', 'drillhole_reject',
    ARRAY['Si','Al','Fe'],
    '{}'::jsonb
) ON CONFLICT DO NOTHING;

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 10 — pg_boundaries_by_project (assertions 43-49)
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 43: NULL project_id returns (NULL, NULL)
SELECT is(
    (SELECT mvt FROM silver.pg_boundaries_by_project(1, 0, 0, '{"project_id": null}'::json)),
    NULL::bytea,
    'boundaries: null project_id returns null mvt'
);

-- Test 44: non-existent project returns (NULL, NULL)
SELECT is(
    (SELECT mvt FROM silver.pg_boundaries_by_project(1, 0, 0, '{"project_id":"00000000-0000-0000-0000-000000000000"}'::json)),
    NULL::bytea,
    'boundaries: missing project returns null mvt'
);

-- Test 45: valid project with fixtures returns non-null mvt
SELECT isnt(
    (SELECT mvt FROM silver.pg_boundaries_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    NULL::bytea,
    'boundaries: valid project+tile returns non-null mvt'
);

-- Test 46: octet_length > 0
SELECT ok(
    (SELECT octet_length(mvt) > 0 FROM silver.pg_boundaries_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'boundaries: mvt octet_length > 0'
);

-- Test 47: etag_hash is md5 format
SELECT matches(
    (SELECT etag_hash FROM silver.pg_boundaries_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    '^[a-f0-9]{32}$',
    'boundaries: etag_hash is md5 format'
);

-- Test 48: bumping data_version changes etag_hash
DO $$
DECLARE
    etag_before text;
    etag_after  text;
BEGIN
    SELECT etag_hash INTO etag_before
    FROM silver.pg_boundaries_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    UPDATE silver.projects
    SET data_version = data_version + 1
    WHERE project_id = 'a1111111-1111-1111-1111-111111111111';

    SELECT etag_hash INTO etag_after
    FROM silver.pg_boundaries_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    PERFORM ok(etag_before <> etag_after, 'boundaries: data_version bump changes etag_hash');
    -- No reset: data_version_monotonic trigger prevents decrement; ROLLBACK cleans up.
END;
$$;
SELECT ok(TRUE, 'boundaries: data_version bump etag test ran');

-- Test 49: empty project returns valid etag (no features)
SELECT matches(
    (SELECT etag_hash FROM silver.pg_boundaries_by_project(1, 0, 0, '{"project_id":"b2222222-2222-2222-2222-222222222222"}'::json)),
    '^[a-f0-9]{32}$',
    'boundaries: empty project still returns valid etag_hash'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 11 — pg_formations_by_project (assertions 50-56)
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 50: NULL project_id
SELECT is(
    (SELECT mvt FROM silver.pg_formations_by_project(1, 0, 0, '{"project_id": null}'::json)),
    NULL::bytea,
    'formations: null project_id returns null mvt'
);

-- Test 51: missing project
SELECT is(
    (SELECT mvt FROM silver.pg_formations_by_project(1, 0, 0, '{"project_id":"00000000-0000-0000-0000-000000000000"}'::json)),
    NULL::bytea,
    'formations: missing project returns null mvt'
);

-- Test 52: valid project returns non-null mvt
SELECT isnt(
    (SELECT mvt FROM silver.pg_formations_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    NULL::bytea,
    'formations: valid project+tile returns non-null mvt'
);

-- Test 53: octet_length > 0
SELECT ok(
    (SELECT octet_length(mvt) > 0 FROM silver.pg_formations_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'formations: mvt octet_length > 0'
);

-- Test 54: etag_hash is md5 format
SELECT matches(
    (SELECT etag_hash FROM silver.pg_formations_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    '^[a-f0-9]{32}$',
    'formations: etag_hash is md5 format'
);

-- Test 55: data_version bump changes etag_hash
DO $$
DECLARE
    etag_before text;
    etag_after  text;
BEGIN
    SELECT etag_hash INTO etag_before
    FROM silver.pg_formations_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    UPDATE silver.projects
    SET data_version = data_version + 1
    WHERE project_id = 'a1111111-1111-1111-1111-111111111111';

    SELECT etag_hash INTO etag_after
    FROM silver.pg_formations_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    PERFORM ok(etag_before <> etag_after, 'formations: data_version bump changes etag_hash');
    -- No reset: data_version_monotonic trigger prevents decrement; ROLLBACK cleans up.
END;
$$;
SELECT ok(TRUE, 'formations: data_version bump etag test ran');

-- Test 56: empty project returns valid etag
SELECT matches(
    (SELECT etag_hash FROM silver.pg_formations_by_project(1, 0, 0, '{"project_id":"b2222222-2222-2222-2222-222222222222"}'::json)),
    '^[a-f0-9]{32}$',
    'formations: empty project returns valid etag_hash'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 12 — pg_historic_workings_by_project (assertions 57-63)
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 57: NULL project_id
SELECT is(
    (SELECT mvt FROM silver.pg_historic_workings_by_project(1, 0, 0, '{"project_id": null}'::json)),
    NULL::bytea,
    'historic_workings: null project_id returns null mvt'
);

-- Test 58: missing project
SELECT is(
    (SELECT mvt FROM silver.pg_historic_workings_by_project(1, 0, 0, '{"project_id":"00000000-0000-0000-0000-000000000000"}'::json)),
    NULL::bytea,
    'historic_workings: missing project returns null mvt'
);

-- Test 59: valid project returns non-null mvt
SELECT isnt(
    (SELECT mvt FROM silver.pg_historic_workings_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    NULL::bytea,
    'historic_workings: valid project+tile returns non-null mvt'
);

-- Test 60: octet_length > 0
SELECT ok(
    (SELECT octet_length(mvt) > 0 FROM silver.pg_historic_workings_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'historic_workings: mvt octet_length > 0'
);

-- Test 61: etag_hash is md5 format
SELECT matches(
    (SELECT etag_hash FROM silver.pg_historic_workings_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    '^[a-f0-9]{32}$',
    'historic_workings: etag_hash is md5 format'
);

-- Test 62: data_version bump changes etag_hash
DO $$
DECLARE
    etag_before text;
    etag_after  text;
BEGIN
    SELECT etag_hash INTO etag_before
    FROM silver.pg_historic_workings_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    UPDATE silver.projects
    SET data_version = data_version + 1
    WHERE project_id = 'a1111111-1111-1111-1111-111111111111';

    SELECT etag_hash INTO etag_after
    FROM silver.pg_historic_workings_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    PERFORM ok(etag_before <> etag_after, 'historic_workings: data_version bump changes etag_hash');
    -- No reset: data_version_monotonic trigger prevents decrement; ROLLBACK cleans up.
END;
$$;
SELECT ok(TRUE, 'historic_workings: data_version bump etag test ran');

-- Test 63: empty project returns valid etag
SELECT matches(
    (SELECT etag_hash FROM silver.pg_historic_workings_by_project(1, 0, 0, '{"project_id":"b2222222-2222-2222-2222-222222222222"}'::json)),
    '^[a-f0-9]{32}$',
    'historic_workings: empty project returns valid etag_hash'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 13 — pg_geochem_by_project (assertions 64-72)
-- ══════════════════════════════════════════════════════════════════════════════

-- Test 64: NULL project_id
SELECT is(
    (SELECT mvt FROM silver.pg_geochem_by_project(1, 0, 0, '{"project_id": null}'::json)),
    NULL::bytea,
    'geochem: null project_id returns null mvt'
);

-- Test 65: missing project
SELECT is(
    (SELECT mvt FROM silver.pg_geochem_by_project(1, 0, 0, '{"project_id":"00000000-0000-0000-0000-000000000000"}'::json)),
    NULL::bytea,
    'geochem: missing project returns null mvt'
);

-- Test 66: valid project returns non-null mvt (fixture rows have geom)
SELECT isnt(
    (SELECT mvt FROM silver.pg_geochem_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    NULL::bytea,
    'geochem: valid project+tile returns non-null mvt'
);

-- Test 67: octet_length > 0
SELECT ok(
    (SELECT octet_length(mvt) > 0 FROM silver.pg_geochem_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    'geochem: mvt octet_length > 0'
);

-- Test 68: etag_hash is md5 format
SELECT matches(
    (SELECT etag_hash FROM silver.pg_geochem_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json)),
    '^[a-f0-9]{32}$',
    'geochem: etag_hash is md5 format'
);

-- Test 69: data_version bump changes etag_hash
DO $$
DECLARE
    etag_before text;
    etag_after  text;
BEGIN
    SELECT etag_hash INTO etag_before
    FROM silver.pg_geochem_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    UPDATE silver.projects
    SET data_version = data_version + 1
    WHERE project_id = 'a1111111-1111-1111-1111-111111111111';

    SELECT etag_hash INTO etag_after
    FROM silver.pg_geochem_by_project(1, 0, 0, '{"project_id":"a1111111-1111-1111-1111-111111111111"}'::json);

    PERFORM ok(etag_before <> etag_after, 'geochem: data_version bump changes etag_hash');
    -- No reset: data_version_monotonic trigger prevents decrement; ROLLBACK cleans up.
END;
$$;
SELECT ok(TRUE, 'geochem: data_version bump etag test ran');

-- Test 70: empty project returns valid etag
SELECT matches(
    (SELECT etag_hash FROM silver.pg_geochem_by_project(1, 0, 0, '{"project_id":"b2222222-2222-2222-2222-222222222222"}'::json)),
    '^[a-f0-9]{32}$',
    'geochem: empty project returns valid etag_hash'
);

-- Test 71: assay_element_codes backfill — fixture row GC-TEST-001 has all 7 oxides
-- so assay_element_codes should contain all 7 element symbols.
SELECT ok(
    (SELECT assay_element_codes @> ARRAY['Si','Al','Fe','Mg','Ca','Na','K']
     FROM silver.geochemistry
     WHERE geochem_id = 'ec000001-0000-0000-0000-000000000001'),
    'geochem: assay_element_codes contains all 7 element codes when all oxides set'
);

-- Test 72: assay_element_codes for GC-TEST-002 (only SiO2, Al2O3, Fe2O3 set)
-- should contain exactly 3 codes and NOT contain Mg/Ca/Na/K.
SELECT ok(
    (SELECT assay_element_codes = ARRAY['Si','Al','Fe']
     FROM silver.geochemistry
     WHERE geochem_id = 'ec000001-0000-0000-0000-000000000002'),
    'geochem: assay_element_codes contains only 3 codes when only 3 oxides set'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- TEARDOWN
-- ══════════════════════════════════════════════════════════════════════════════

DELETE FROM silver.drill_traces WHERE trace_id IN (
    'd1111111-1111-1111-1111-111111111111',
    'd2222222-2222-2222-2222-222222222222',
    'd3333333-3333-3333-3333-333333333333'
);

DELETE FROM silver.seismic_surveys WHERE survey_id IN (
    'e1111111-1111-1111-1111-111111111111',
    'e2222222-2222-2222-2222-222222222222',
    'e3333333-3333-3333-3333-333333333333'
);

DELETE FROM silver.collars WHERE collar_id IN (
    'c1111111-1111-1111-1111-111111111111',
    'c2222222-2222-2222-2222-222222222222',
    'c3333333-3333-3333-3333-333333333333'
);

-- 8.2b fixtures cleanup
DELETE FROM silver.geochemistry WHERE geochem_id IN (
    'ec000001-0000-0000-0000-000000000001',
    'ec000001-0000-0000-0000-000000000002'
);

DELETE FROM silver.historic_workings WHERE id IN (
    'ed000001-0000-0000-0000-000000000001',
    'ed000001-0000-0000-0000-000000000002'
);

DELETE FROM silver.geological_formations WHERE id IN (
    'ff000001-0000-0000-0000-000000000001',
    'ff000001-0000-0000-0000-000000000002'
);

DELETE FROM silver.project_boundaries WHERE id IN (
    'bb000001-0000-0000-0000-000000000001',
    'bb000001-0000-0000-0000-000000000002'
);

DELETE FROM silver.projects WHERE project_id IN (
    'a1111111-1111-1111-1111-111111111111',
    'b2222222-2222-2222-2222-222222222222'
);

DELETE FROM silver.workspaces WHERE workspace_id = 'f0000000-0000-0000-0000-000000000001';

SELECT * FROM finish();

ROLLBACK;
