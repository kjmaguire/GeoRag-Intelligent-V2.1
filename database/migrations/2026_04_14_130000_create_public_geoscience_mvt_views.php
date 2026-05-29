<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 3.3 — PostGIS views for Martin MVT tile serving.
 *
 * Plan §09a locks Martin + dedicated views over the canonical Public Geoscience
 * tables, not base-table auto-publish. Views give:
 *   • a stable tile contract decoupled from future column additions on base tables
 *   • a clean place to apply jurisdiction scoping + license-visibility rules
 *   • column selection (we drop the heavy `source_attributes` JSONB and
 *     `source_geom_wkt` TEXT — they're useful for detail views, not tile payloads).
 *
 *   v_pg_mines_mvt                → pg_mine
 *   v_pg_mineral_occurrences_mvt  → pg_mineral_occurrence
 *   v_pg_drillhole_collars_mvt    → pg_drillhole_collar
 *   v_pg_resource_potential_mvt   → pg_resource_potential_zone
 *
 * Each view is consumed by Martin via `table_sources` in
 * docker/martin/martin.yaml. Martin wraps each view's geometry with ST_AsMVT
 * internally.
 *
 * Per-zoom column selection (plan §09a) is handled client-side in MapLibre
 * via paint expressions — we deliberately ship a flat, stable view schema so
 * the tile contract doesn't depend on the client's zoom level.
 */
return new class extends Migration
{
    public function up(): void
    {
        // ── v_pg_mines_mvt ───────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE OR REPLACE VIEW public_geo.v_pg_mines_mvt AS
            SELECT
                m.id,
                m.jurisdiction_code,
                m.source_id,
                m.source_feature_id,
                m.name,
                m.status,
                m.commodities,
                m.commodity_grouping,
                m.operator,
                m.source_url,
                m.last_seen_at,
                m.geom
              FROM public_geo.pg_mine m
        SQL);

        DB::statement("COMMENT ON VIEW public_geo.v_pg_mines_mvt IS 'MVT tile source for Martin. Flat projection of pg_mine minus source_attributes/source_geom_wkt. Consumed by /tiles/public-geoscience/pg_mines.'");

        // ── v_pg_mineral_occurrences_mvt ─────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE OR REPLACE VIEW public_geo.v_pg_mineral_occurrences_mvt AS
            SELECT
                o.id,
                o.jurisdiction_code,
                o.source_id,
                o.source_feature_id,
                o.smdi_id,
                o.name,
                o.status,
                o.primary_commodities,
                o.associated_commodities,
                o.commodity_grouping,
                o.discovery_type,
                o.production_flag,
                o.source_url,
                o.last_seen_at,
                o.geom
              FROM public_geo.pg_mineral_occurrence o
        SQL);

        DB::statement("COMMENT ON VIEW public_geo.v_pg_mineral_occurrences_mvt IS 'MVT tile source for Martin. Flat projection of pg_mineral_occurrence.'");

        // ── v_pg_drillhole_collars_mvt ───────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE OR REPLACE VIEW public_geo.v_pg_drillhole_collars_mvt AS
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
                d.total_length_m,
                d.core_availability,
                d.last_seen_at,
                d.geom
              FROM public_geo.pg_drillhole_collar d
        SQL);

        DB::statement("COMMENT ON VIEW public_geo.v_pg_drillhole_collars_mvt IS 'MVT tile source for Martin. Flat projection of pg_drillhole_collar; drops stratigraphic_depths JSONB + source_attributes for tile payload weight.'");

        // ── v_pg_resource_potential_mvt ──────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE OR REPLACE VIEW public_geo.v_pg_resource_potential_mvt AS
            SELECT
                r.id,
                r.jurisdiction_code,
                r.source_id,
                r.source_feature_id,
                r.commodity,
                r.commodity_grouping,
                r.potential_rank,
                r.methodology_ref,
                r.last_seen_at,
                r.geom
              FROM public_geo.pg_resource_potential_zone r
        SQL);

        DB::statement("COMMENT ON VIEW public_geo.v_pg_resource_potential_mvt IS 'MVT tile source for Martin. MultiPolygon features with commodity + potential_rank for choropleth styling.'");
    }

    public function down(): void
    {
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_resource_potential_mvt');
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_drillhole_collars_mvt');
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_mineral_occurrences_mvt');
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_mines_mvt');
    }
};
