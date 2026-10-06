<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Terrain-model fallback for collars whose file carried no elevation (§04e).
 *
 * A collar file without an RL column lands every collar with
 * silver.collars.elevation NULL, and every 3D reader then draws the hole at
 * z = 0 — RedStar's five Unga Island trenches sat at sea level for that reason
 * (project diagnostics, 2026-10-06). Kyle asked the same day for the gap to be
 * filled from a terrain model.
 *
 * The terrain value gets its OWN columns rather than being written into
 * `elevation`, so:
 *   - a surveyed elevation that arrives later (a re-upload with an RL column)
 *     wins without anything having to clear a flag;
 *   - every reader that has not opted in (exports, agent tools,
 *     silver.mv_collar_summary) still sees NULL — what the file actually said;
 *   - the 3D readers use COALESCE(elevation, elevation_dem_m).
 *
 * Columns, all NULL until promote_silver_to_gold looks the collar up
 * (src/fastapi/app/services/dem_elevation.py):
 *   elevation_dem_m      ground height from the model, metres above the
 *                        EGM2008 geoid. NULL with elevation_dem_geom set means
 *                        "looked up; the model has no ground here" (open sea).
 *   elevation_dem_source which model answered (default copernicus_glo30).
 *   elevation_dem_geom   the position the lookup was made AT. A collar that
 *                        has since moved (re-uploaded under its correct CRS)
 *                        no longer matches geom_4326 and is looked up again.
 *
 * Purely additive; no policy or grant changes — the existing collars RLS
 * policies govern the new columns like every other.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.collars
              ADD COLUMN IF NOT EXISTS elevation_dem_m      real,
              ADD COLUMN IF NOT EXISTS elevation_dem_source varchar(32),
              ADD COLUMN IF NOT EXISTS elevation_dem_geom   geometry(Point, 4326)
        SQL);

        // Below the Dead Sea shore to above Everest: a value outside it is a
        // decoding fault (a nodata sentinel read as a height), not terrain.
        DB::statement('ALTER TABLE silver.collars DROP CONSTRAINT IF EXISTS chk_collars_elevation_dem_range');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.collars
              ADD CONSTRAINT chk_collars_elevation_dem_range
              CHECK (elevation_dem_m IS NULL OR elevation_dem_m BETWEEN -500 AND 9000)
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.collars.elevation_dem_m IS
              'Ground height at the collar from a terrain model (m above the EGM2008 geoid), written by promote_silver_to_gold only while `elevation` is NULL. Never replaces `elevation`: 3D readers use COALESCE(elevation, elevation_dem_m). NULL with elevation_dem_geom set = looked up, no ground in the model there.'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.collars.elevation_dem_source IS
              'Terrain model that produced elevation_dem_m (COLLAR_DEM_SOURCE; default copernicus_glo30).'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.collars.elevation_dem_geom IS
              'Position the terrain lookup was made at. When it no longer equals geom_4326 the collar has moved and is looked up again.'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.collars DROP CONSTRAINT IF EXISTS chk_collars_elevation_dem_range');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.collars
              DROP COLUMN IF EXISTS elevation_dem_geom,
              DROP COLUMN IF EXISTS elevation_dem_source,
              DROP COLUMN IF EXISTS elevation_dem_m
        SQL);
    }
};
