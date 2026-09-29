<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Retire silver.collars.geom (§04e — Kyle, 2026-09-29).
 *
 * WHY
 *   `geom` was geometry(Point, 32613): every collar on earth stored in UTM
 *   zone 13N, which breaks the platform's EPSG:4326-at-rest rule and was the
 *   root of a string of wrong-place bugs (ST_SRID(geom) read as the source
 *   CRS put Alaskan traces 2,500 km east; the tabular writer could not write
 *   a single non-13N collar until it was conformed). Its WGS84 twin
 *   `geom_4326` (2026_08_19_010000) is transformed at insert straight from
 *   each collar's SOURCE CRS and is what every reader already used: the
 *   collar tile source (2026_09_29_200100), drill traces, significant
 *   intersections, coverage density, the agent tools, promotion, the
 *   exports and the Laravel collar API. Every writer now writes geom_4326
 *   alone.
 *
 * WHAT GOES
 *   - trg_collars_derive_geom_4326 + silver.collars_derive_geom_4326():
 *     they derived geom_4326 FROM geom, which no longer exists. A writer that
 *     forgets geom_4326 now leaves it NULL, exactly as a writer that forgot
 *     both did before — tests/test_collar_geom_srid_conformance.py pins
 *     every writer to setting it.
 *   - idx_collars_geom (GIST on geom). idx_collars_geom_4326 stays.
 *   - the column itself. Plain DROP COLUMN, deliberately NOT CASCADE: if an
 *     out-of-band view somewhere still depends on `geom`, this fails loudly
 *     instead of silently dropping that view with it.
 *
 * CRS at every hop after this: source file -> geom_4326 (ingest) ->
 * EPSG:4326 at rest -> 3857 in the Martin functions; metric math uses
 * geography or the collar's own UTM zone, never a pinned 32613.
 *
 * DOWN recreates `geom` as geometry(Point, 32613) populated from geom_4326,
 * plus its index, the derive function and the trigger, as 2026_08_19_010000
 * left them. PROJ's extended Transverse Mercator round-trips 32613 far
 * outside the zone, so the restored values match what the column held.
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

        DB::statement('DROP TRIGGER IF EXISTS trg_collars_derive_geom_4326 ON silver.collars');
        DB::statement('DROP FUNCTION IF EXISTS silver.collars_derive_geom_4326()');
        DB::statement('DROP INDEX IF EXISTS silver.idx_collars_geom');
        DB::statement('ALTER TABLE silver.collars DROP COLUMN IF EXISTS geom');

        DB::statement("COMMENT ON COLUMN silver.collars.geom_4326 IS
            'The collar position, EPSG:4326, transformed at insert straight from the source CRS. The only collar geometry: the EPSG:32613 geom column was retired 2026-09-29 (2026_09_30_100000). easting/northing hold the untouched source values.'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $hasColumn = DB::selectOne(
            "SELECT 1 AS present FROM information_schema.columns
              WHERE table_schema = 'silver' AND table_name = 'collars'
                AND column_name = 'geom'",
        );

        if (! $hasColumn) {
            // The same typmod column AddGeometryColumn(..., 32613, 'POINT', 2)
            // created in 2026_04_09_180100.
            DB::statement('ALTER TABLE silver.collars ADD COLUMN geom geometry(Point, 32613)');
        }

        DB::statement(
            'UPDATE silver.collars
                SET geom = ST_Transform(geom_4326, 32613)
              WHERE geom_4326 IS NOT NULL
                AND geom IS NULL',
        );

        DB::statement('CREATE INDEX IF NOT EXISTS idx_collars_geom ON silver.collars USING GIST (geom)');

        DB::unprepared(<<<'SQL'
            CREATE OR REPLACE FUNCTION silver.collars_derive_geom_4326()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $fn$
            BEGIN
                IF NEW.geom IS NULL OR ST_SRID(NEW.geom) = 0 THEN
                    RETURN NEW;
                END IF;

                IF NEW.geom_4326 IS NULL THEN
                    NEW.geom_4326 := ST_Transform(NEW.geom, 4326);
                ELSIF TG_OP = 'UPDATE'
                      AND NEW.geom IS DISTINCT FROM OLD.geom
                      AND NEW.geom_4326 IS NOT DISTINCT FROM OLD.geom_4326 THEN
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

        DB::statement('COMMENT ON COLUMN silver.collars.geom_4326 IS NULL');
    }
};
