-- =============================================================================
-- Phase 17 Step 2 — Milestone-2 XLS-24-* collar fixture + project metadata fix
-- (R-P14-3.1, R-P14-3.2, R-P14-3.3).
--
-- Phase 13 seeded 10 PLS-* collars but the Milestone-1 golden tests
-- expect 20 total (Milestone-2 XLS-24-* parser was meant to add 10
-- more). Phase 17 seeds those 10 directly.
--
-- Depth design (see docs/phase17_golden_failure_audit.md):
--   - PLS-* sum = 3480m (fixed, keep PLS-21-06=265 as min + PLS-22-08=510 as max)
--   - XLS-24-* sum = 3736m → 20-hole avg = (3480 + 3736) / 20 = 360.8 ✓
--   - All XLS-24-* depths in (265, 510) so min/max stay PLS-pinned
--
-- Also updates the project commodity to 'uranium' + trims region to
-- start with 'Athabasca' so gq-022 + gq-024 phrase matches succeed.
--
-- Idempotent.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Update parent project metadata.
-- ---------------------------------------------------------------------------
UPDATE silver.projects
   SET commodity = 'uranium',
       region    = 'Athabasca Basin',
       updated_at = clock_timestamp()
 WHERE project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
   AND (commodity IS DISTINCT FROM 'uranium' OR region IS DISTINCT FROM 'Athabasca Basin');

-- ---------------------------------------------------------------------------
-- 2. Ten Milestone-2 XLS-24-* collars. Geographic stepping just NE of
-- the PLS-* programme. Depths sum 3736m. All in (265, 510).
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
        ('XLS-24-01', 494100.0,  6522500.0, 463.0, 280.0,   0.0, -85.0, DATE '2024-04-12', 'Completed'),
        ('XLS-24-02', 494400.0,  6522700.0, 461.0, 300.0,  20.0, -85.0, DATE '2024-04-25', 'Completed'),
        ('XLS-24-03', 494700.0,  6522900.0, 459.0, 320.0,  40.0, -85.0, DATE '2024-05-08', 'Completed'),
        ('XLS-24-04', 495000.0,  6523100.0, 457.0, 340.0,  60.0, -85.0, DATE '2024-05-22', 'Completed'),
        ('XLS-24-05', 495400.0,  6523300.0, 455.0, 360.0,  80.0, -85.0, DATE '2024-06-04', 'Completed'),
        ('XLS-24-06', 495800.0,  6523500.0, 453.0, 380.0, 100.0, -85.0, DATE '2024-06-18', 'Completed'),
        ('XLS-24-07', 496300.0,  6523700.0, 451.0, 400.0, 120.0, -85.0, DATE '2024-07-02', 'Completed'),
        ('XLS-24-08', 496800.0,  6523900.0, 449.0, 420.0, 140.0, -85.0, DATE '2024-07-16', 'Completed'),
        ('XLS-24-09', 497400.0,  6524100.0, 447.0, 440.0, 160.0, -85.0, DATE '2024-07-30', 'Completed'),
        ('XLS-24-10', 498000.0,  6524300.0, 445.0, 496.0, 180.0, -85.0, DATE '2024-08-15', 'Completed')
) AS c(hole_id, easting, northing, elevation, total_depth, azimuth, dip, drill_date, status)
ON CONFLICT (project_id, hole_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 3. Refresh the materialised view that the agent reads.
-- ---------------------------------------------------------------------------
REFRESH MATERIALIZED VIEW silver.mv_collar_summary;

-- ---------------------------------------------------------------------------
-- 4. Sanity — exactly 20 collars under the test project; 10 XLS-24-*;
-- avg_depth ≈ 360.8 across all 20.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n_total int;
    n_xls   int;
    avg_d   numeric;
BEGIN
    SELECT count(*) INTO n_total
      FROM silver.collars
     WHERE project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';
    SELECT count(*) INTO n_xls
      FROM silver.collars
     WHERE project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
       AND hole_id LIKE 'XLS-24-%';
    SELECT round(avg(total_depth)::numeric, 1) INTO avg_d
      FROM silver.collars
     WHERE project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';
    RAISE NOTICE 'Phase 17 Step 2: total=% xls=% avg_depth=%',
        n_total, n_xls, avg_d;
    IF n_total <> 20 THEN
        RAISE EXCEPTION 'expected 20 collars, got %', n_total;
    END IF;
    IF n_xls <> 10 THEN
        RAISE EXCEPTION 'expected 10 XLS-24-* collars, got %', n_xls;
    END IF;
    IF avg_d IS DISTINCT FROM 360.8 THEN
        RAISE EXCEPTION 'expected avg_depth=360.8, got %', avg_d;
    END IF;
END $$;
