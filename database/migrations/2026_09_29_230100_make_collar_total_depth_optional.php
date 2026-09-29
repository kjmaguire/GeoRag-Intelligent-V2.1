<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Schema;

/**
 * §04e: silver.collars.total_depth is optional (SME-approved, Kyle,
 * 2026-09-29).
 *
 * The column was NOT NULL (2026_04_09_180100_create_collars_table.php:22)
 * with `chk_total_depth_positive CHECK (total_depth > 0)`
 * (2026_04_13_100000_database_hardening.php:49-53). A collar table without
 * an EOH column therefore could not land at all, and the writers papered over
 * it three different ways — 0.0 (refused by the CHECK), a 0.01 m floor (an
 * invented depth), or skipping the collar. Now an absent depth is NULL and
 * every writer stores NULL, never 0.
 *
 * The > 0 CHECK stays: `total_depth > 0` is satisfied by NULL, so it still
 * refuses a zero or negative depth without refusing an absent one.
 *
 * Readers that need a length fall back to the deepest survey station or
 * interval on record (src/fastapi/app/services/collar_depth.py; the
 * desurvey/strip-log views via their `extendTo` / deepest-interval logic).
 *
 * down(): SET NOT NULL fails once a NULL has been stored, and inventing a
 * depth to satisfy it is exactly what this migration stopped. So when NULLs
 * exist the NOT NULL is expressed as `CHECK (total_depth IS NOT NULL) NOT
 * VALID` — new rows are refused, the stored ones are kept — with a warning
 * naming the count. up() removes that CHECK again, so up/down/up is clean.
 */
return new class extends Migration
{
    private const NOT_NULL_FALLBACK = 'chk_collars_total_depth_not_null';

    public function up(): void
    {
        $driver = DB::connection()->getDriverName();

        if ($driver === 'pgsql') {
            DB::statement('ALTER TABLE silver.collars DROP CONSTRAINT IF EXISTS '.self::NOT_NULL_FALLBACK);
            DB::statement('ALTER TABLE silver.collars ALTER COLUMN total_depth DROP NOT NULL');
            DB::statement(<<<'SQL'
                COMMENT ON COLUMN silver.collars.total_depth IS
                'End-of-hole depth in metres, > 0 when present. NULL when the source did not record one (never 0) - optional since 2026-09-29 (§04e, SME-approved). Readers fall back to the deepest survey/interval.'
            SQL);

            return;
        }

        if ($driver === 'sqlite') {
            Schema::table('collars', function (Blueprint $table): void {
                $table->float('total_depth')->nullable()->change();
            });
        }
    }

    public function down(): void
    {
        $driver = DB::connection()->getDriverName();

        if ($driver === 'pgsql') {
            $nulls = (int) DB::scalar('SELECT count(*) FROM silver.collars WHERE total_depth IS NULL');

            if ($nulls === 0) {
                DB::statement('ALTER TABLE silver.collars ALTER COLUMN total_depth SET NOT NULL');

                return;
            }

            DB::statement(
                'ALTER TABLE silver.collars ADD CONSTRAINT '.self::NOT_NULL_FALLBACK
                .' CHECK (total_depth IS NOT NULL) NOT VALID',
            );
            Log::warning('silver.collars.total_depth left nullable: collars without a depth kept', [
                'collars_without_total_depth' => $nulls,
                'constraint' => self::NOT_NULL_FALLBACK.' (NOT VALID)',
            ]);

            return;
        }

        if ($driver === 'sqlite') {
            Schema::table('collars', function (Blueprint $table): void {
                $table->float('total_depth')->nullable(false)->change();
            });
        }
    }
};
