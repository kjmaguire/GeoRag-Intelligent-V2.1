-- =============================================================================
-- Phase 18 Step 5 — fix silver.mv_collar_summary to use DISTINCT joins.
--
-- Phase 18 Step 2 surfaced a pre-existing bug in the MV definition:
--   LEFT JOIN samples + lithology_logs multiplies collar rows by the
--   cartesian product of matched samples and lithology intervals before
--   the GROUP BY. count(c.collar_id) and avg(c.total_depth) then over-
--   count any collar that has downhole data.
--
-- Before Phase 18, silver.samples + silver.lithology_logs were empty
-- for the test project so this bug was invisible. After Phase 18
-- seeds 4 + 4 rows, total_collars jumps from 20 → 26 and avg_depth
-- skews from 360.8 → 373.3.
--
-- Fix: rebuild the MV using count(DISTINCT) on the collar key and
-- aggregate-from-subquery for depth metrics so each collar is counted
-- once. Sample + lithology counts stay row-based on their own tables.
--
-- Cannot CREATE OR REPLACE a materialized view; DROP CASCADE then
-- re-create with identical column shape. The MV's index needs
-- re-creation too.
--
-- Idempotent: DROP IF EXISTS + CREATE handles re-runs.
-- =============================================================================

DROP MATERIALIZED VIEW IF EXISTS silver.mv_collar_summary CASCADE;

CREATE MATERIALIZED VIEW silver.mv_collar_summary AS
SELECT
    c.project_id,
    count(DISTINCT c.collar_id)                AS total_collars,
    avg(c.total_depth)::numeric(10,1)          AS avg_depth,
    min(c.total_depth)::numeric(10,1)          AS min_depth,
    max(c.total_depth)::numeric(10,1)          AS max_depth,
    count(DISTINCT c.hole_type)                AS hole_type_count,
    min(c.drill_date)                          AS earliest_drill,
    max(c.drill_date)                          AS latest_drill,
    (SELECT count(*) FROM silver.samples s
        JOIN silver.collars c2 ON c2.collar_id = s.collar_id
        WHERE c2.project_id = c.project_id)    AS total_samples,
    (SELECT count(*) FROM silver.lithology_logs ll
        JOIN silver.collars c3 ON c3.collar_id = ll.collar_id
        WHERE c3.project_id = c.project_id)    AS total_litho_intervals
FROM silver.collars c
GROUP BY c.project_id;

CREATE UNIQUE INDEX IF NOT EXISTS mv_collar_summary_project_id_idx
    ON silver.mv_collar_summary (project_id);

REFRESH MATERIALIZED VIEW silver.mv_collar_summary;

DO $$
DECLARE
    n_collars int;
    n_avg     numeric;
    n_samp    int;
    n_litho   int;
BEGIN
    SELECT total_collars, avg_depth, total_samples, total_litho_intervals
      INTO n_collars, n_avg, n_samp, n_litho
      FROM silver.mv_collar_summary
     WHERE project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d';
    RAISE NOTICE 'Phase 18 MV fix: collars=% avg=% samples=% litho=%',
        n_collars, n_avg, n_samp, n_litho;
    IF n_collars <> 20 THEN
        RAISE EXCEPTION 'expected total_collars=20 after fix, got %', n_collars;
    END IF;
    IF n_avg <> 360.8 THEN
        RAISE EXCEPTION 'expected avg_depth=360.8 after fix, got %', n_avg;
    END IF;
END $$;
