<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * Sub-step progress for the Ingestion Runs UI. The bar was quantized to
 * step_index/total_steps (5 steps -> 0/20/40/60/80/100) and sat at 40%
 * through the whole multi-minute parse. stage_pct carries fractional
 * progress WITHIN the current step (0..1) and stage_detail a short
 * human string ("page 214/482", "embedded 1,024/3,984").
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            DB::statement('ALTER TABLE silver.ingest_progress ADD COLUMN IF NOT EXISTS stage_pct real');
            DB::statement('ALTER TABLE silver.ingest_progress ADD COLUMN IF NOT EXISTS stage_detail text');

            return;
        }

        if (Schema::hasTable('ingest_progress') && ! Schema::hasColumn('ingest_progress', 'stage_pct')) {
            Schema::table('ingest_progress', function ($table) {
                $table->float('stage_pct')->nullable();
                $table->text('stage_detail')->nullable();
            });
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            DB::statement('ALTER TABLE silver.ingest_progress DROP COLUMN IF EXISTS stage_pct');
            DB::statement('ALTER TABLE silver.ingest_progress DROP COLUMN IF EXISTS stage_detail');

            return;
        }

        if (Schema::hasTable('ingest_progress') && Schema::hasColumn('ingest_progress', 'stage_pct')) {
            Schema::table('ingest_progress', function ($table) {
                $table->dropColumn(['stage_pct', 'stage_detail']);
            });
        }
    }
};
