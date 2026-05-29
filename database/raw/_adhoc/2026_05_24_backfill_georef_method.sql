-- CC-01 Item 2 — backfill georef_method + crs_confidence on silver.collars
-- and silver.spatial_features. The schema landed via
--   2026_05_23_050000_add_spatial_uncertainty_to_collars_and_spatial_features.php
-- which left both columns NULL on every existing row. This script populates
-- a conservative default so the MapView uncertainty-rings layer has
-- something to render before the ingestion pipeline starts emitting the
-- provenance fields natively.
--
-- Backfill rules (per CC-01 follow-on plan):
--
--   1. Rows with a real geometry (geom IS NOT NULL) and no georef_method →
--        georef_method  = 'detected'
--        crs_confidence = 0.7
--      We trust the geometry was assigned by the spatial pipeline; the
--      conservative 0.7 says "this was inferred, not stated."
--
--   2. Rows with raw easting/northing only (no geom) →
--        georef_method  = 'assumed'
--        crs_confidence = 0.3
--      The 0.3 communicates that the UTM zone was almost certainly inferred
--      from project bbox, not declared in the source PDF.
--
--   3. Rows already carrying a georef_method are left alone.
--
-- silver.spatial_features mirrors rule 1 only — it has no easting/northing
-- columns, so rule 2 doesn't apply.
--
-- spatial_uncertainty_m is intentionally NOT backfilled. The migration COMMENT
-- says NULL means "not recorded; UI map ring is omitted" — which is exactly
-- the right behaviour for rows that have never been measured. Operators who
-- need rings on legacy data should set the radius per-source (e.g. the
-- typical georeferencing-error budget for a given report) rather than
-- letting this script invent a number.
--
-- Run as:
--   docker exec -i georag-postgresql psql -U georag -d georag \
--     < database/raw/_adhoc/2026_05_24_backfill_georef_method.sql
--
-- Idempotent. Re-running is a no-op because every UPDATE filters on
-- georef_method IS NULL.

\set ON_ERROR_STOP on

BEGIN;

-- ── silver.collars ────────────────────────────────────────────────────────

-- Capture before counts so the report at the end shows the actual delta.
CREATE TEMP TABLE _cc01_audit_before ON COMMIT DROP AS
SELECT
    (SELECT COUNT(*) FROM silver.collars
        WHERE georef_method IS NULL AND geom IS NOT NULL)                              AS collars_geom_null_method,
    (SELECT COUNT(*) FROM silver.collars
        WHERE georef_method IS NULL AND geom IS NULL
          AND easting IS NOT NULL AND northing IS NOT NULL)                            AS collars_en_null_method,
    (SELECT COUNT(*) FROM silver.spatial_features
        WHERE georef_method IS NULL AND geom IS NOT NULL)                              AS sf_geom_null_method;

-- Rule 1 (collars): geom present, method missing → detected / 0.7
WITH updated AS (
    UPDATE silver.collars
       SET georef_method  = 'detected',
           crs_confidence = COALESCE(crs_confidence, 0.7)
     WHERE georef_method IS NULL
       AND geom IS NOT NULL
    RETURNING 1
)
SELECT COUNT(*) AS collars_detected_rows FROM updated \gset

-- Rule 2 (collars): only easting/northing → assumed / 0.3
WITH updated AS (
    UPDATE silver.collars
       SET georef_method  = 'assumed',
           crs_confidence = COALESCE(crs_confidence, 0.3)
     WHERE georef_method IS NULL
       AND geom IS NULL
       AND easting IS NOT NULL
       AND northing IS NOT NULL
    RETURNING 1
)
SELECT COUNT(*) AS collars_assumed_rows FROM updated \gset

-- ── silver.spatial_features ───────────────────────────────────────────────

-- Rule 1 (spatial_features): geom present, method missing → detected / 0.7
WITH updated AS (
    UPDATE silver.spatial_features
       SET georef_method  = 'detected',
           crs_confidence = COALESCE(crs_confidence, 0.7)
     WHERE georef_method IS NULL
       AND geom IS NOT NULL
    RETURNING 1
)
SELECT COUNT(*) AS spatial_features_detected_rows FROM updated \gset

-- ── Audit report ──────────────────────────────────────────────────────────

SELECT
    b.collars_geom_null_method  AS before_collars_geom_null_method,
    :collars_detected_rows      AS updated_collars_to_detected,
    b.collars_en_null_method    AS before_collars_en_null_method,
    :collars_assumed_rows       AS updated_collars_to_assumed,
    b.sf_geom_null_method       AS before_spatial_features_geom_null_method,
    :spatial_features_detected_rows AS updated_spatial_features_to_detected
FROM _cc01_audit_before b;

-- Sanity check: every targeted row should now have a non-null method. If the
-- two columns below come back > 0, the backfill missed something and the
-- transaction will roll back so the operator can investigate.
DO $$
DECLARE
    leftover_collars     bigint;
    leftover_spatial     bigint;
BEGIN
    SELECT COUNT(*) INTO leftover_collars
      FROM silver.collars
     WHERE georef_method IS NULL
       AND (geom IS NOT NULL
            OR (easting IS NOT NULL AND northing IS NOT NULL));

    SELECT COUNT(*) INTO leftover_spatial
      FROM silver.spatial_features
     WHERE georef_method IS NULL
       AND geom IS NOT NULL;

    IF leftover_collars > 0 OR leftover_spatial > 0 THEN
        RAISE EXCEPTION
            'CC-01 backfill incomplete: % collars + % spatial_features still NULL after update',
            leftover_collars, leftover_spatial;
    END IF;
END
$$;

COMMIT;
