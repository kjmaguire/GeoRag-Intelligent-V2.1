-- =============================================================================
-- Phase 13 Step 3 — Milestone-1 golden-query fixture seed
-- (R-P11-baseline-1).
--
-- The Milestone-1 golden test suite at
-- src/fastapi/tests/test_golden_queries.py expects 10 collars under
-- project 019d74a1-fba8-7165-9ae6-a5bf93eef97d. Phase 11 captured
-- the baseline at 2 / 35 passing — agent correctly refused on the
-- missing fixture. This migration seeds the project + 10 collars so
-- the agent has real data to retrieve.
--
-- See docs/phase13_golden_fixture_spec.md for column-by-column
-- rationale, including the deliberate easting min/max + status mix
-- that the test assertions hinge on.
--
-- Idempotent: ON CONFLICT DO NOTHING on both INSERTs.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Parent project row.
-- ---------------------------------------------------------------------------
INSERT INTO silver.projects (
    project_id,
    project_name,
    crs_datum,
    crs_epsg,
    company,
    orientation_reference,
    commodity,
    region,
    status,
    slug,
    workspace_id,
    data_version,
    created_at,
    updated_at
)
VALUES (
    '019d74a1-fba8-7165-9ae6-a5bf93eef97d',
    'Phantom Lake Silver',
    'EPSG:32613',
    32613,
    'Phantom Lake Mining Ltd.',
    'grid',
    'silver',
    'Athabasca Basin, Northern Saskatchewan',
    'active',
    'phantom-lake-silver',
    NULL,
    0,
    clock_timestamp(),
    clock_timestamp()
)
ON CONFLICT (project_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 2. Ten Milestone-1 collars. UTM Zone 13N (EPSG:32613) coordinates;
-- ST_SetSRID(ST_MakePoint(easting, northing), 32613) for geom.
-- ---------------------------------------------------------------------------
INSERT INTO silver.collars (
    collar_id, hole_id, project_id, easting, northing, elevation,
    total_depth, hole_type, azimuth, dip, drill_date, status,
    geom, geom_4326, hole_id_canonical, created_at, updated_at
)
SELECT
    gen_random_uuid()                                     AS collar_id,
    c.hole_id,
    '019d74a1-fba8-7165-9ae6-a5bf93eef97d'::uuid          AS project_id,
    c.easting,
    c.northing,
    c.elevation,
    c.total_depth,
    'Diamond'                                             AS hole_type,
    c.azimuth,
    c.dip,
    c.drill_date,
    c.status,
    ST_SetSRID(ST_MakePoint(c.easting, c.northing), 32613) AS geom,
    ST_Transform(
        ST_SetSRID(ST_MakePoint(c.easting, c.northing), 32613),
        4326
    )                                                     AS geom_4326,
    c.hole_id                                             AS hole_id_canonical,
    clock_timestamp(),
    clock_timestamp()
FROM (
    VALUES
        ('PLS-20-01', 494100.0,  6520200.0, 480.0, 320.0,   0.0, -90.0, DATE '2020-06-10', 'Completed'),
        ('PLS-20-02', 494300.0,  6520400.0, 482.0, 340.0,   0.0, -90.0, DATE '2020-07-04', 'Completed'),
        ('PLS-20-03', 494500.0,  6520600.0, 485.0, 360.0,   0.0, -90.0, DATE '2020-07-22', 'Completed'),
        ('PLS-20-04', 494700.0,  6520800.0, 488.0, 290.0,   0.0, -90.0, DATE '2020-08-15', 'Completed'),
        ('PLS-21-05', 493445.0,  6521000.0, 495.0, 380.0,  45.0, -75.0, DATE '2021-05-18', 'Completed'),
        ('PLS-21-06', 495200.0,  6521200.0, 478.0, 265.0,  90.0, -60.0, DATE '2021-06-25', 'Completed'),
        ('PLS-21-07', 495500.0,  6521400.0, 475.0, 305.0, 135.0, -75.0, DATE '2021-08-02', 'Completed'),
        ('PLS-22-08', 496000.0,  6521600.0, 470.0, 510.0, 180.0, -90.0, DATE '2022-06-14', 'Completed'),
        ('PLS-22-09', 496800.0,  6521800.0, 468.0, 370.0, 225.0, -75.0, DATE '2022-07-29', 'Completed'),
        ('PLS-22-10', 498256.9,  6522000.0, 465.0, 340.0, 270.0, -60.0, DATE '2022-08-30', 'In Progress')
) AS c(hole_id, easting, northing, elevation, total_depth, azimuth, dip, drill_date, status)
ON CONFLICT (project_id, hole_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. Refresh the materialised view the agent reads to build its
-- "HIGH-CONFIDENCE SUMMARIES" prompt block (Phase 14 R-P13-1 fix).
-- Without this, the agent's orchestrator finds no row for the test
-- project in silver.mv_collar_summary, omits the summaries block,
-- and the LLM responds "I don't have that number in this project."
-- See docs/phase14_r-p13-1_scoping.md for the full diagnosis.
-- ---------------------------------------------------------------------------
REFRESH MATERIALIZED VIEW silver.mv_collar_summary;

-- ---------------------------------------------------------------------------
-- 4. Sanity check — exactly 10 collars under the test project after
-- the migration. RAISE NOTICE for the operator; don't abort if the
-- row count differs (the table may be intentionally extended by
-- the Milestone-2 XLS-24-* parser later).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n_collars int;
    n_pls int;
BEGIN
    SELECT count(*) INTO n_collars
      FROM silver.collars
     WHERE project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';
    SELECT count(*) INTO n_pls
      FROM silver.collars
     WHERE project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
       AND hole_id LIKE 'PLS-%';
    RAISE NOTICE 'Phase 13 Step 3: total=% PLS-prefix=%',
        n_collars, n_pls;
    IF n_pls <> 10 THEN
        RAISE EXCEPTION 'Phase 13 Step 3: expected 10 PLS-* collars, got %', n_pls;
    END IF;
END $$;
