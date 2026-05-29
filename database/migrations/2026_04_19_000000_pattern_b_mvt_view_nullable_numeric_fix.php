<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Pattern B (see docs/mvt-nullable-numeric-convention.md) for MVT views
 * whose underlying canonical columns contain NULLs in a typed-numeric or
 * typed-smallint column. MapLibre's tile-parsing worker rejects an entire
 * tile when a typed property arrives as null, so we:
 *
 *   a) COALESCE the underlying column to a safe sentinel (0 / 0::smallint)
 *   b) expose a paired `has_<col>` boolean so the frontend can still
 *      distinguish "zero recorded" from "no data".
 *
 * Historical context: a one-off `scripts/fix_drillhole_mvt_view.sql` was
 * applied to the dev database at convention-establishment time, but the
 * change was never promoted to a migration. Fresh installs (including
 * `georag_test` provisioned by phpunit.pgsql.xml) therefore produced
 * views without the paired `has_*` columns, and
 * MvtViewNullNumericRegressionTest::test_known_pattern_b_pairs_exist
 * failed against a freshly-migrated DB. This migration brings the
 * migration tree in sync with dev so RefreshDatabase test DBs match.
 *
 * Note: `date_drilled` is intentionally omitted from the drillhole view —
 * the dev DB does not expose it and neither Martin tile config nor the
 * frontend popup references it. If we need the attribute on tiles later,
 * add it via a follow-up migration alongside a COALESCE/has_date_drilled
 * pair (date columns have the same null-in-typed-property behaviour).
 */
return new class extends Migration
{
    public function up(): void
    {
        // DROP + CREATE rather than CREATE OR REPLACE because the new view
        // shape drops `date_drilled` and changes column order — Postgres
        // only allows CREATE OR REPLACE VIEW to append columns at the end.

        // ── v_pg_drillhole_collars_mvt ───────────────────────────────────
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_drillhole_collars_mvt');
        DB::statement(<<<'SQL'
            CREATE VIEW public_geo.v_pg_drillhole_collars_mvt AS
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
                d.commodity_of_interest,
                COALESCE(d.total_length_m, 0::numeric(10,2)) AS total_length_m,
                (d.total_length_m IS NOT NULL)               AS has_total_length,
                d.core_availability,
                d.last_seen_at,
                d.geom
              FROM public_geo.pg_drillhole_collar d
        SQL);

        DB::statement("COMMENT ON VIEW public_geo.v_pg_drillhole_collars_mvt IS 'MVT tile source for Martin. Pattern B applied: total_length_m is COALESCEd to 0 with paired has_total_length bool.'");

        // ── v_pg_resource_potential_mvt ──────────────────────────────────
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_resource_potential_mvt');
        DB::statement(<<<'SQL'
            CREATE VIEW public_geo.v_pg_resource_potential_mvt AS
            SELECT
                r.id,
                r.jurisdiction_code,
                r.source_id,
                r.source_feature_id,
                r.commodity,
                r.commodity_grouping,
                COALESCE(r.potential_rank, 0::smallint) AS potential_rank,
                (r.potential_rank IS NOT NULL)          AS has_potential_rank,
                r.methodology_ref,
                r.last_seen_at,
                r.geom
              FROM public_geo.pg_resource_potential_zone r
        SQL);

        DB::statement("COMMENT ON VIEW public_geo.v_pg_resource_potential_mvt IS 'MVT tile source for Martin. Pattern B applied: potential_rank is COALESCEd to 0 with paired has_potential_rank bool.'");
    }

    public function down(): void
    {
        // Restore the original view definitions from 2026_04_14_130000.
        // This reverts Pattern B — the frontend will start rejecting tiles
        // again on any row with a null total_length_m / potential_rank.
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_drillhole_collars_mvt');
        DB::statement(<<<'SQL'
            CREATE VIEW public_geo.v_pg_drillhole_collars_mvt AS
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

        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_resource_potential_mvt');
        DB::statement(<<<'SQL'
            CREATE VIEW public_geo.v_pg_resource_potential_mvt AS
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
    }
};
