-- =============================================================================
-- Phase 18 Step 2 + 3 — assay + lithology golden fixtures (R-P14-3.5).
--
-- Phase 17 unlocked 15/31 golden tests by getting the collar + project
-- metadata right. The next layer is downhole data:
--   gq-014 expects U3O8 + "52" in the response → assay fixture needed
--   gq-015 expects "PLS-20-01" + "SST" + "PGN"   → lithology fixture
--   gq-017 expects "Au"                           → Au assay key
--
-- Schema gotcha: silver.samples.workspace_id is NOT NULL. The Phase 13
-- silver.projects seed set workspace_id NULL; this migration links the
-- project to the default workspace (a0000000-...0001) so the samples
-- share it.
--
-- Idempotent: ON CONFLICT DO NOTHING on inserts; UPDATE is a no-op once
-- workspace_id matches.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Link the test project to the default workspace so silver.samples
-- inserts have a valid workspace_id to share.
-- ---------------------------------------------------------------------------
UPDATE silver.projects
   SET workspace_id = 'a0000000-0000-0000-0000-000000000001',
       updated_at   = clock_timestamp()
 WHERE project_id   = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
   AND workspace_id IS DISTINCT FROM 'a0000000-0000-0000-0000-000000000001';

-- ---------------------------------------------------------------------------
-- 2. Assay samples on PLS-22-08 (the deepest hole, often surfaced by the
-- agent). Four core samples spanning 350-450m with U3O8 mineralisation
-- peaking at 52,000 ppm. Two of the four also carry Au_ppb so gq-017's
-- "Au" phrase match succeeds.
--
-- The agent's `query_assay_data` tool aggregates across commodity_assays
-- keys; with these four rows, peak U3O8 = 52000 ppm, peak Au = 410 ppb.
-- ---------------------------------------------------------------------------
INSERT INTO silver.samples (
    sample_id, collar_id, from_depth, to_depth, sample_type, lab_id,
    commodity_assays, workspace_id, created_at, updated_at
)
SELECT
    gen_random_uuid()                                AS sample_id,
    c.collar_id,
    s.from_depth,
    s.to_depth,
    'Core'                                           AS sample_type,
    s.lab_id,
    s.commodity_assays::jsonb,
    'a0000000-0000-0000-0000-000000000001'::uuid     AS workspace_id,
    clock_timestamp(),
    clock_timestamp()
FROM (
    -- (hole_id, from_depth, to_depth, lab_id, assays_json)
    VALUES
        ('PLS-22-08', 350.0, 365.0, 'SRC-2022-08-1',
            '{"U3O8_ppm": 18500, "Au_ppb": 22}'),
        ('PLS-22-08', 365.0, 380.0, 'SRC-2022-08-2',
            '{"U3O8_ppm": 52000, "Au_ppb": 410}'),
        ('PLS-22-08', 380.0, 395.0, 'SRC-2022-08-3',
            '{"U3O8_ppm": 41200}'),
        ('PLS-22-08', 395.0, 410.0, 'SRC-2022-08-4',
            '{"U3O8_ppm": 6800}')
) AS s(hole_id, from_depth, to_depth, lab_id, commodity_assays)
JOIN silver.collars c ON c.hole_id = s.hole_id
                     AND c.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'::uuid
WHERE NOT EXISTS (
    SELECT 1 FROM silver.samples ss
     WHERE ss.collar_id = c.collar_id
       AND ss.from_depth = s.from_depth
       AND ss.to_depth   = s.to_depth
);

-- ---------------------------------------------------------------------------
-- 3. Lithology log for PLS-20-01 — realistic Athabasca-Basin-style strip.
-- Codes:
--   OVB = Overburden (till + soil)
--   SST = Athabasca Sandstone (above the unconformity)
--   PGN = Paragneiss (basement rocks)
--   GNT = Granitic intrusion
-- gq-015 expects "PLS-20-01" + "SST" + "PGN" in the agent's response.
-- ---------------------------------------------------------------------------
INSERT INTO silver.lithology_logs (
    log_id, collar_id, from_depth, to_depth,
    lithology_code, lithology_description,
    grain_size, color, hardness, rqd, recovery, weathering,
    created_at, updated_at
)
SELECT
    gen_random_uuid()                                AS log_id,
    c.collar_id,
    l.from_depth, l.to_depth,
    l.lithology_code, l.lithology_description,
    l.grain_size, l.color, l.hardness,
    l.rqd, l.recovery, l.weathering,
    clock_timestamp(), clock_timestamp()
FROM (
    VALUES
        (  0.0,  50.0, 'OVB', 'Glacial till and overburden',     NULL,        'brown',      'soft',   NULL::float,  95.0, 'severe'),
        ( 50.0, 200.0, 'SST', 'Athabasca Sandstone',             'medium',    'pink-white', 'medium', 82.0::float,  95.0, 'fresh'),
        (200.0, 300.0, 'PGN', 'Basement paragneiss with biotite','fine',      'grey',       'hard',   88.0::float,  98.0, 'fresh'),
        (300.0, 320.0, 'GNT', 'Granitic intrusion',              'coarse',    'pink',       'hard',   91.0::float,  99.0, 'fresh')
) AS l(from_depth, to_depth, lithology_code, lithology_description, grain_size, color, hardness, rqd, recovery, weathering)
JOIN silver.collars c ON c.hole_id = 'PLS-20-01'
                     AND c.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'::uuid
WHERE NOT EXISTS (
    SELECT 1 FROM silver.lithology_logs ll
     WHERE ll.collar_id = c.collar_id
       AND ll.from_depth = l.from_depth
       AND ll.to_depth   = l.to_depth
);

-- ---------------------------------------------------------------------------
-- 4. Refresh the materialised view (Phase 14 R-P13-1 fix). silver.mv_collar_summary
-- carries total_samples + total_litho_intervals fields that the agent's
-- HIGH-CONFIDENCE SUMMARIES block surfaces.
-- ---------------------------------------------------------------------------
REFRESH MATERIALIZED VIEW silver.mv_collar_summary;

-- ---------------------------------------------------------------------------
-- 5. Sanity checks — 4 samples on PLS-22-08, 4 lithology intervals on
-- PLS-20-01, mv_collar_summary picks up the new counts.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    n_samples       int;
    n_litho         int;
    mv_samples      int;
    mv_litho        int;
BEGIN
    SELECT count(*) INTO n_samples
      FROM silver.samples s
      JOIN silver.collars c ON c.collar_id = s.collar_id
     WHERE c.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
       AND c.hole_id    = 'PLS-22-08';
    SELECT count(*) INTO n_litho
      FROM silver.lithology_logs ll
      JOIN silver.collars c ON c.collar_id = ll.collar_id
     WHERE c.project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'
       AND c.hole_id    = 'PLS-20-01';
    SELECT total_samples, total_litho_intervals
      INTO mv_samples, mv_litho
      FROM silver.mv_collar_summary
     WHERE project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';

    RAISE NOTICE 'Phase 18 fixture: samples=% litho=% mv_samples=% mv_litho=%',
        n_samples, n_litho, mv_samples, mv_litho;

    IF n_samples < 4 THEN
        RAISE EXCEPTION 'expected ≥4 samples on PLS-22-08, got %', n_samples;
    END IF;
    IF n_litho < 4 THEN
        RAISE EXCEPTION 'expected ≥4 lithology intervals on PLS-20-01, got %', n_litho;
    END IF;
END $$;
