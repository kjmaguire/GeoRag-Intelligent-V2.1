-- Fix drillhole MVT view: coalesce NULL total_length_m to 0 so MapLibre's
-- tile worker doesn't reject the whole tile on null-in-typed-numeric.
-- Adds has_total_length bool so the popup can distinguish "0 m recorded"
-- from "no depth available".
CREATE OR REPLACE VIEW public_geoscience.v_pg_drillhole_collars_mvt AS
SELECT
    d.id,
    d.jurisdiction_code,
    d.source_id,
    d.source_feature_id,
    d.drillhole_id,
    d.drillhole_name,
    d.company,
    d.project_name,
    d.drill_type,
    d.date_drilled,
    d.commodity_of_interest,
    COALESCE(d.total_length_m, 0::numeric(10,2)) AS total_length_m,
    (d.total_length_m IS NOT NULL)               AS has_total_length,
    d.core_availability,
    d.last_seen_at,
    d.geom
FROM public_geoscience.pg_drillhole_collar d;
