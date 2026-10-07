<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.collars.elevation_dem_m: real -> double precision.
 *
 * 2026_10_06_110000 created the terrain height as `real` (float4) while
 * `elevation` beside it is double precision. COALESCE(elevation,
 * elevation_dem_m) promotes the float4, and a stored 12.9 comes back as
 * 12.899999618530273 — into the Workspace payload, the trace geometry's z and
 * the trace digest. Same type as `elevation` removes the mismatch.
 *
 * Existing values are float4 renderings of heights the lookup rounds to a
 * centimetre, so they are rounded back to two decimals on the way across
 * rather than carrying the float4 tail into the new column.
 *
 * The range CHECK is on the column and is re-evaluated by the type change; a
 * value the old type held always satisfies it. No view reads the column.
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
              ALTER COLUMN elevation_dem_m TYPE double precision
              USING round(elevation_dem_m::numeric, 2)::double precision
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.collars
              ALTER COLUMN elevation_dem_m TYPE real
        SQL);
    }
};
