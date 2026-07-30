<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * Minimal SQLite mirrors for raw-SQL tables traversed by project deletion.
 */
return new class extends Migration
{
    /** @var list<string> */
    private const TABLES = [
        'raster_layers',
        'answer_runs',
        'geophysics_surveys',
        'mineral_claims',
        'review_queue',
        'campaigns',
        'zone_statistics',
        'element_correlations',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        foreach (self::TABLES as $tableName) {
            if (Schema::hasTable($tableName)) {
                continue;
            }

            Schema::create($tableName, function (Blueprint $table): void {
                $table->id();
                $table->uuid('project_id')->nullable();
            });
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        foreach (array_reverse(self::TABLES) as $tableName) {
            Schema::dropIfExists($tableName);
        }
    }
};
