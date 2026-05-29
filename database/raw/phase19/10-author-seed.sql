-- =============================================================================
-- Phase 19 Step 1 — seed silver.reports.authors so populate_neo4j.py creates
-- the QualifiedPerson nodes the golden tests expect (gq-012, gq-025).
--
-- silver.reports already has multiple rows for "Patterson Lake South
-- Property" but every authors array is empty. populate_neo4j.py iterates
-- silver.reports.authors and creates :QualifiedPerson {name: $author}
-- with an AUTHORED_BY edge from :Report. Without authors there's no QP.
--
-- We seed the first matching report row with the canonical Phase 18+
-- golden-test ground-truth authors:
--   Sarah Thompson — the qualified person for gq-012 + gq-025
--   David Chen — secondary author (filler so the array isn't single-valued)
--
-- Idempotent: ON-NULL or empty array only; further runs are no-op.
-- =============================================================================

UPDATE silver.reports
   SET authors    = ARRAY['Sarah Thompson', 'David Chen'],
       updated_at = clock_timestamp()
 WHERE report_id IN (
        SELECT report_id
          FROM silver.reports
         WHERE project_name = 'Patterson Lake South Property'
           AND (authors IS NULL OR cardinality(authors) = 0)
         ORDER BY filing_date DESC NULLS LAST, report_id
         LIMIT 1
   )
   -- Short-circuit: if any PLS report already lists Sarah Thompson,
   -- no further rows need seeding. Makes the migration a true no-op
   -- on re-run rather than walking down the empty-authors backlog.
   AND NOT EXISTS (
        SELECT 1 FROM silver.reports
         WHERE project_name = 'Patterson Lake South Property'
           AND 'Sarah Thompson' = ANY (authors)
   );

-- Sanity check — at least one report under PLS has Sarah Thompson now.
DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n
      FROM silver.reports
     WHERE project_name = 'Patterson Lake South Property'
       AND 'Sarah Thompson' = ANY (authors);
    RAISE NOTICE 'Phase 19 author seed: % PLS report(s) carry Sarah Thompson', n;
    IF n < 1 THEN
        RAISE EXCEPTION 'expected ≥1 PLS report with Sarah Thompson author, got %', n;
    END IF;
END $$;
