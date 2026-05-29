-- pgTAP Golden MVT Snapshot Tests — Module 8 Chunk 8.8 Deliverable B
-- File: database/tests/pgtap/10_golden_mvt_snapshots.sql
--
-- Run: bash database/tests/pgtap/run.sh --filter 10
-- Requires:
--   1. pgTAP extension installed in the georag database.
--   2. GoldenFixture seed loaded (seed_golden_fixture.sql applied idempotently).
--
-- These tests lock the byte-for-byte MVT output of all 7 silver MVT functions
-- for a deterministic fixed tile (z=3, x=1, y=2) against a known fixture project.
-- A failing snapshot means a function's output changed — either intentionally
-- (regen required) or as a regression (investigate before merging).
--
-- Fixture project : 00000000-0000-0000-0000-deadbeefcafe  (GoldenFixture)
-- Tile            : z=3, x=1, y=2  (lon -135 to -90, lat ~41 to ~67)
-- data_version    : 1
--
-- Golden hashes captured: 2026-04-26
-- Reference manifest    : database/tests/pgtap/golden/manifest.json
--
-- To regen after intentional function or fixture change:
--   bash database/tests/pgtap/golden/generate.sh
--   Update the md5 constants below to match the new manifest.
--   Commit manifest.json and this file together with a regen justification.
--
-- Test inventory (21 assertions):
--   BLOCK 1 — Seed guard (7 assertions)
--   BLOCK 2 — MVT byte snapshot, one per layer (7 assertions)
--   BLOCK 3 — Determinism: two identical calls produce identical MVT (7 assertions)
--
-- Total assertions: 21

BEGIN;

SELECT plan(21);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 1 — Seed guard
-- Asserts that the fixture data is loaded before running snapshot assertions.
-- Failure here means seed_golden_fixture.sql was not applied.
-- ══════════════════════════════════════════════════════════════════════════════

SELECT ok(
    (SELECT COUNT(*) FROM silver.collars WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe') >= 3,
    'GoldenFixture: at least 3 collar rows present (seed guard)'
);

SELECT ok(
    (SELECT COUNT(*) FROM silver.drill_traces WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe') >= 3,
    'GoldenFixture: at least 3 drill trace rows present (seed guard)'
);

SELECT ok(
    (SELECT COUNT(*) FROM silver.project_boundaries WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe') >= 1,
    'GoldenFixture: at least 1 boundary row present (seed guard)'
);

SELECT ok(
    (SELECT COUNT(*) FROM silver.geological_formations WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe') >= 1,
    'GoldenFixture: at least 1 formation row present (seed guard)'
);

SELECT ok(
    (SELECT COUNT(*) FROM silver.historic_workings WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe') >= 2,
    'GoldenFixture: at least 2 historic workings rows present (seed guard)'
);

SELECT ok(
    (SELECT COUNT(*) FROM silver.seismic_surveys WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe') >= 1,
    'GoldenFixture: at least 1 seismic survey row present (seed guard)'
);

SELECT ok(
    (SELECT COUNT(*) FROM silver.geochemistry WHERE project_id = '00000000-0000-0000-0000-deadbeefcafe') >= 3,
    'GoldenFixture: at least 3 geochemistry rows present (seed guard)'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 2 — MVT byte snapshot assertions
--
-- Each assertion compares md5(mvt) against the golden hash captured on
-- 2026-04-26 from the same fixture. All hashes are in manifest.json.
--
-- Tile: z=3, x=1, y=2  (lon -135 to -90, lat ~41 to ~67)
-- Project: 00000000-0000-0000-0000-deadbeefcafe  (GoldenFixture, data_version=1)
--
-- Note: All 7 etag_hash values are identical (5e649996...) because they share
-- the formula md5(data_version|z|x|y|project_id). This is correct and expected.
-- The MVT byte md5 values DIFFER per layer (different feature sets and geometry).
-- ══════════════════════════════════════════════════════════════════════════════

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_collars_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    'e1aa3e412a19a56cb6810df932eee48e',
    'pg_collars_by_project(3,1,2): MVT bytes match golden snapshot'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_drill_traces_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    '2a5b60dcbd6a677c142ef61f68016f7d',
    'pg_drill_traces_by_project(3,1,2): MVT bytes match golden snapshot'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_seismic_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    '9bd62f37ae939dfb4bcd1fa48e183327',
    'pg_seismic_by_project(3,1,2): MVT bytes match golden snapshot'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_boundaries_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    '2681fbee28a088b4f9116f04855d3818',
    'pg_boundaries_by_project(3,1,2): MVT bytes match golden snapshot'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_formations_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    '7a95cd4517f105a2f030f2fa54c9c203',
    'pg_formations_by_project(3,1,2): MVT bytes match golden snapshot'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_historic_workings_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    '30b392f09f0bb7bf3d4ea4650163d90e',
    'pg_historic_workings_by_project(3,1,2): MVT bytes match golden snapshot'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_geochem_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    '4be83161da76152f072a9df8c5f844df',
    'pg_geochem_by_project(3,1,2): MVT bytes match golden snapshot'
);

-- ══════════════════════════════════════════════════════════════════════════════
-- BLOCK 3 — Determinism assertions
--
-- Two identical calls at the same tile must produce bit-identical MVT output.
-- Verifies that ORDER BY <pk> is correctly applied before ST_AsMVT in all
-- 7 functions — a missing ORDER BY can cause non-deterministic row ordering.
-- ══════════════════════════════════════════════════════════════════════════════

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_collars_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    (SELECT md5(mvt) FROM silver.pg_collars_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    'pg_collars_by_project: MVT output is deterministic (two identical calls)'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_drill_traces_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    (SELECT md5(mvt) FROM silver.pg_drill_traces_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    'pg_drill_traces_by_project: MVT output is deterministic (two identical calls)'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_seismic_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    (SELECT md5(mvt) FROM silver.pg_seismic_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    'pg_seismic_by_project: MVT output is deterministic (two identical calls)'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_boundaries_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    (SELECT md5(mvt) FROM silver.pg_boundaries_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    'pg_boundaries_by_project: MVT output is deterministic (two identical calls)'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_formations_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    (SELECT md5(mvt) FROM silver.pg_formations_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    'pg_formations_by_project: MVT output is deterministic (two identical calls)'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_historic_workings_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    (SELECT md5(mvt) FROM silver.pg_historic_workings_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    'pg_historic_workings_by_project: MVT output is deterministic (two identical calls)'
);

SELECT is(
    (SELECT md5(mvt) FROM silver.pg_geochem_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    (SELECT md5(mvt) FROM silver.pg_geochem_by_project(
        3, 1, 2, '{"project_id": "00000000-0000-0000-0000-deadbeefcafe"}'::json
    ) WHERE mvt IS NOT NULL),
    'pg_geochem_by_project: MVT output is deterministic (two identical calls)'
);

SELECT * FROM finish();

ROLLBACK;
