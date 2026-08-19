<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Declares `silver.collars.geom_4326` — the WGS84 twin of `geom` — and keeps
 * it populated.
 *
 * WHY THIS EXISTS
 * ---------------
 * As of 2026-08-19 eleven production code paths read or write this column:
 *
 *   - App\Http\Controllers\Foundry\WorkspaceController (show + holePayload)
 *   - src/fastapi/app/agent/tools.py                (spatial retrieval tool)
 *   - src/fastapi/app/agent/agentic_retrieval/nodes.py
 *   - src/fastapi/app/routers/visualizations.py     (/v1/viz)
 *   - src/fastapi/app/services/completeness_audit.py
 *   - src/fastapi/app/services/ingest/{cameco_log,las,csv_collar}_ingester.py
 *   - src/dagster/.../assets/gold_cross_section_panels.py
 *   - 2026_05_20_061000_create_martin_significant_intersections_function
 *   - 2026_05_23_060000_create_coverage_density_function
 *
 * ...and NOTHING created it. 2026_04_09_180100_create_collars_table adds only
 * `geom` (EPSG:32613 via AddGeometryColumn). The column exists in the dev and
 * Azure databases because it was added out of band; it has never been in
 * version control. The proof is georag_test, which is built from migrations
 * alone and has no geom_4326 at all — every Workspace request against it dies
 * with `column "geom_4326" does not exist`, and a freshly provisioned cluster
 * would do the same.
 *
 * THE TRIGGER
 * -----------
 * Declaring the column is only half of it. Two of the five collar writers —
 * Dagster's CSV (assets/silver.py) and XLSX (assets/silver_xlsx.py)
 * INSERT_COLLAR_SQL — set `geom` and never `geom_4326`, and their
 * ON CONFLICT DO UPDATE never refreshes it either. A collar ingested through
 * either path lands with geom_4326 NULL, and NULL is not a cosmetic gap here:
 * it is silently dropped by every consumer above. Such a hole does not plot
 * on the Workspace map, is excluded from spatial RAG answers (tools.py filters
 * `co.geom_4326 IS NOT NULL`), never reaches a cross-section panel, and is
 * counted as incomplete by the coverage audit. Data was imported; the UI just
 * never showed it.
 *
 * A trigger rather than five patched INSERT statements, because the failure
 * mode is "a writer forgot" and that recurs — this is the same reasoning as
 * the bronze.provenance workspace_id autofill trigger (2026-05-25).
 *
 * It fills ONLY when geom_4326 is NULL, so the three FastAPI ingesters keep
 * their own value: those transform straight from the source CRS (Cameco is
 * EPSG:32155 → 4326) whereas deriving from `geom` would round-trip through
 * 32613 and lose a little precision. The one case where an existing value is
 * replaced is an UPDATE that moved `geom` while leaving geom_4326 untouched —
 * otherwise the pair would silently disagree, which is worse than either.
 *
 * A generated column would be the obvious alternative and is not available:
 * PostGIS ST_Transform is STABLE, not IMMUTABLE, so it cannot appear in a
 * GENERATED ALWAYS AS expression.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            // The SQLite fast suite has no PostGIS; its collars table carries
            // no geometry at all.
            return;
        }

        // AddGeometryColumn is not idempotent, hence the explicit guard: on
        // dev and on Azure the column is already there and this migration
        // must be a no-op rather than an error.
        $hasColumn = DB::selectOne(
            "SELECT 1 AS present FROM information_schema.columns
              WHERE table_schema = 'silver' AND table_name = 'collars'
                AND column_name = 'geom_4326'",
        );

        if (! $hasColumn) {
            DB::statement("SELECT AddGeometryColumn('silver', 'collars', 'geom_4326', 4326, 'POINT', 2)");
        }

        DB::statement('CREATE INDEX IF NOT EXISTS idx_collars_geom_4326 ON silver.collars USING GIST (geom_4326)');

        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.collars_derive_geom_4326()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $fn$
            BEGIN
                -- Nothing to derive from, or an SRID-less geometry that
                -- ST_Transform would reject. Returning NEW unchanged keeps a
                -- malformed row's own error message rather than replacing it
                -- with a confusing one from this trigger.
                IF NEW.geom IS NULL OR ST_SRID(NEW.geom) = 0 THEN
                    RETURN NEW;
                END IF;

                IF NEW.geom_4326 IS NULL THEN
                    NEW.geom_4326 := ST_Transform(NEW.geom, 4326);
                ELSIF TG_OP = 'UPDATE'
                      AND NEW.geom IS DISTINCT FROM OLD.geom
                      AND NEW.geom_4326 IS NOT DISTINCT FROM OLD.geom_4326 THEN
                    -- geom moved but the caller left geom_4326 alone: refresh
                    -- it rather than let the pair drift apart.
                    NEW.geom_4326 := ST_Transform(NEW.geom, 4326);
                END IF;

                RETURN NEW;
            END;
            $fn$;
        SQL);

        DB::unprepared(<<<'SQL'
            DROP TRIGGER IF EXISTS trg_collars_derive_geom_4326 ON silver.collars;
            CREATE TRIGGER trg_collars_derive_geom_4326
                BEFORE INSERT OR UPDATE OF geom, geom_4326 ON silver.collars
                FOR EACH ROW
                EXECUTE FUNCTION silver.collars_derive_geom_4326();
        SQL);

        // Existing rows the Dagster paths left behind. Bounded work: this is
        // a drill-collar table, thousands of rows rather than millions.
        DB::statement(
            'UPDATE silver.collars
                SET geom_4326 = ST_Transform(geom, 4326)
              WHERE geom IS NOT NULL
                AND geom_4326 IS NULL
                AND ST_SRID(geom) <> 0',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TRIGGER IF EXISTS trg_collars_derive_geom_4326 ON silver.collars');
        DB::statement('DROP FUNCTION IF EXISTS silver.collars_derive_geom_4326()');

        // The column itself is deliberately NOT dropped. It predates this
        // migration in every deployed environment and eleven code paths read
        // it; dropping it on rollback would break far more than this
        // migration ever added.
    }
};
