-- CC-01 Item 2 — backfill spatial_uncertainty_m via georef_method rule table
-- (Kyle approved Strategy B: per-tier midpoint values, 2026-05-24)
--
-- Approved uncertainty tiers (midpoint of range):
--   modern_ni43101_survey   : modern (post-2010) NI 43-101 + signed survey  →  7 m (5-10)
--   modern_ni43101_declared : modern (post-2010) NI 43-101 + declared CRS   → 35 m (25-50)
--   legacy_declared         : pre-2010, declared CRS                         → 75 m (50-100)
--   legacy_assumed_utm      : pre-2010, assumed UTM zone                     → 175 m (100-250)
--   hand_digitised          : digitised from published map                   → 350 m (200-500)
--   government_gps          : government-survey GPS waypoint                 →  10 m (5-15)
--
-- georef_method vocabulary (from chk_collars_georef_method):
--   'declared'  → CRS was explicitly stated in the source document
--   'detected'  → CRS was inferred by the spatial pipeline from geometry
--   'assumed'   → only easting/northing available, UTM zone inferred from bbox
--   'manual'    → operator-entered coordinates, no survey instrument
--   'survey'    → professional GPS/DGPS survey
--
-- Era logic: pre-2010 vs post-2010 uses COALESCE(drill_date, created_at).
-- A NULL date is treated conservatively as pre-2010 (higher uncertainty).
--
-- The spatial_uncertainty_method VARCHAR(100) column is added if absent so
-- every updated row carries an auditable rule name.
--
-- Run as:
--   docker cp database/raw/_adhoc/2026_05_30_backfill_spatial_uncertainty.sql \
--             georag-postgresql:/tmp/
--   docker exec georag-postgresql psql -U georag -d georag \
--             -f /tmp/2026_05_30_backfill_spatial_uncertainty.sql
--
-- Idempotent: the UPDATE filters on spatial_uncertainty_m IS NULL so
-- re-running is a no-op on already-backfilled rows.

\set ON_ERROR_STOP on

BEGIN;

-- ── 1. Add spatial_uncertainty_method audit column (idempotent) ───────────

ALTER TABLE silver.collars
    ADD COLUMN IF NOT EXISTS spatial_uncertainty_method VARCHAR(100);

COMMENT ON COLUMN silver.collars.spatial_uncertainty_method IS
    'CC-01 Item 2 — rule name used to derive spatial_uncertainty_m. '
    'NULL when uncertainty was set by a direct measurement or not yet assigned. '
    'One of: modern_ni43101_survey, modern_ni43101_declared, legacy_declared, '
    'legacy_assumed_utm, hand_digitised, government_gps.';

-- ── 2. Capture before-counts for the audit report ─────────────────────────

CREATE TEMP TABLE _su_audit_before ON COMMIT DROP AS
SELECT COUNT(*) AS collars_null_uncertainty
  FROM silver.collars
 WHERE spatial_uncertainty_m IS NULL
   AND georef_method IS NOT NULL;

-- ── 3. Apply rule table UPDATE ────────────────────────────────────────────
--
-- Rule table as a VALUES CTE:
--   (georef_method, is_modern, uncertainty_m, method_name)
--
-- "is_modern" = true when COALESCE(drill_date, created_at) >= 2010-01-01.
-- For 'survey' and 'manual' the era column is effectively ignored
-- (survey → 10 m regardless; manual → same as assumed-era bucket).
--
-- Precedence (most specific wins via CASE ordering):
--   1. survey        → government_gps (10 m)
--   2. declared + modern → modern_ni43101_declared (35 m)
--   3. declared + legacy → legacy_declared (75 m)
--   4. detected + modern → modern_ni43101_declared (35 m)
--      (detected means the pipeline inferred the CRS — treat like declared)
--   5. detected + legacy → legacy_declared (75 m)
--   6. assumed + modern  → legacy_assumed_utm (175 m)
--      (assumed = UTM inferred from bbox; modern doesn't reduce the ambiguity)
--   7. assumed + legacy  → legacy_assumed_utm (175 m)
--   8. manual  + modern  → modern_ni43101_declared (35 m)
--      (manual entry with a visible GPS read; treat like declared, modern)
--   9. manual  + legacy  → legacy_declared (75 m)
--
-- The 'hand_digitised' tier (350 m) is intentionally not wired to any
-- current georef_method value — it would require a future 'digitised'
-- vocab extension.  A NOTE is logged at the end if any rows would qualify.

WITH rules AS (
    -- era flag derived inline so the rule table is a pure VALUES expression
    SELECT
        collar_id,
        georef_method,
        COALESCE(drill_date::date, created_at::date) >= DATE '2010-01-01' AS is_modern,
        CASE
            WHEN georef_method = 'survey'
                THEN 10.0
            WHEN georef_method IN ('declared', 'detected', 'manual')
             AND COALESCE(drill_date::date, created_at::date) >= DATE '2010-01-01'
                THEN 35.0
            WHEN georef_method IN ('declared', 'detected', 'manual')
             AND (COALESCE(drill_date::date, created_at::date) < DATE '2010-01-01'
                  OR COALESCE(drill_date::date, created_at::date) IS NULL)
                THEN 75.0
            WHEN georef_method = 'assumed'
                THEN 175.0
            ELSE NULL  -- unmapped method: do not guess
        END AS computed_uncertainty,
        CASE
            WHEN georef_method = 'survey'
                THEN 'government_gps'
            WHEN georef_method IN ('declared', 'detected', 'manual')
             AND COALESCE(drill_date::date, created_at::date) >= DATE '2010-01-01'
                THEN 'modern_ni43101_declared'
            WHEN georef_method IN ('declared', 'detected', 'manual')
             AND (COALESCE(drill_date::date, created_at::date) < DATE '2010-01-01'
                  OR COALESCE(drill_date::date, created_at::date) IS NULL)
                THEN 'legacy_declared'
            WHEN georef_method = 'assumed'
                THEN 'legacy_assumed_utm'
            ELSE NULL
        END AS method_name
    FROM silver.collars
    WHERE spatial_uncertainty_m IS NULL
      AND georef_method IS NOT NULL
),
updated AS (
    UPDATE silver.collars c
       SET spatial_uncertainty_m      = r.computed_uncertainty,
           spatial_uncertainty_method = r.method_name
      FROM rules r
     WHERE c.collar_id        = r.collar_id
       AND r.computed_uncertainty IS NOT NULL
    RETURNING c.collar_id
)
SELECT COUNT(*) AS rows_updated FROM updated \gset

-- ── 4. Sanity check ───────────────────────────────────────────────────────
-- Every collar that had a known georef_method must now have a non-NULL
-- spatial_uncertainty_m.  If any remain NULL the transaction rolls back.

DO $$
DECLARE
    leftover bigint;
BEGIN
    SELECT COUNT(*)
      INTO leftover
      FROM silver.collars
     WHERE spatial_uncertainty_m IS NULL
       AND georef_method IS NOT NULL;

    IF leftover > 0 THEN
        RAISE EXCEPTION
            'CC-01 backfill incomplete: % collar(s) with georef_method still have '
            'NULL spatial_uncertainty_m — check for unmapped georef_method values',
            leftover;
    END IF;
END
$$;

-- ── 5. Audit report ───────────────────────────────────────────────────────

-- 5a. Summary line
SELECT
    b.collars_null_uncertainty   AS before_null_uncertainty,
    :rows_updated                AS rows_backfilled
FROM _su_audit_before b;

-- 5b. Per-rule breakdown
SELECT
    spatial_uncertainty_method  AS rule_name,
    COUNT(*)                    AS collar_count,
    spatial_uncertainty_m       AS uncertainty_m
FROM silver.collars
WHERE spatial_uncertainty_method IS NOT NULL
GROUP BY spatial_uncertainty_method, spatial_uncertainty_m
ORDER BY uncertainty_m;

-- 5c. Histogram: count of rows in each uncertainty bucket
SELECT
    bucket,
    COUNT(*) AS collar_count
FROM (
    SELECT
        CASE
            WHEN spatial_uncertainty_m <=  10 THEN '(0,10]'
            WHEN spatial_uncertainty_m <=  50 THEN '(10,50]'
            WHEN spatial_uncertainty_m <= 100 THEN '(50,100]'
            WHEN spatial_uncertainty_m <= 500 THEN '(100,500]'
            ELSE                                   '(500,inf)'
        END AS bucket
    FROM silver.collars
    WHERE spatial_uncertainty_m IS NOT NULL
) _hist
GROUP BY bucket
ORDER BY
    CASE bucket
        WHEN '(0,10]'    THEN 1
        WHEN '(10,50]'   THEN 2
        WHEN '(50,100]'  THEN 3
        WHEN '(100,500]' THEN 4
        ELSE                  5
    END;

COMMIT;
