<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Grant DELETE on every table ProjectController::destroy() deletes from
 * directly, plus silver.projects itself, to georag_app.
 *
 * georag_app — the role Laravel's `pgsql` connection uses — only ever had
 * SELECT/INSERT/UPDATE on these tables; DELETE was never granted because
 * nothing needed it until project deletion. Confirmed live 2026-08-17 via
 * a direct reproduction of destroy(): every one of the tables below threw
 * `SQLSTATE[42501]: permission denied` in sequence as each prior grant
 * was applied — project deletion was 100% broken for a second,
 * independent reason on top of the missing-table bug fixed in
 * 3a06ec9 (2026_08_17_030000 is unrelated; that commit didn't touch
 * grants). Same fix shape as
 * 2026_08_17_020000_grant_delete_on_document_passages_to_georag_app and
 * 2026_05_18_120000_grant_delete_on_derive_tables_to_georag_app.
 *
 * Empirically verified live that the FK-cascade children (silver.collars,
 * drill_traces, exports, project_user, project_boundaries,
 * geological_formations, historic_workings, saved_map_views,
 * targeting.target_candidate_zones, silver.ingest_progress — all
 * `ON DELETE CASCADE` to silver.projects) do NOT need their own DELETE
 * grant: PostgreSQL performs referential-integrity cascade actions via
 * internal triggers that aren't subject to the acting role's table
 * privileges. Only tables destroy() issues an explicit DELETE against
 * need a grant, which is exactly the list below.
 *
 * Skipped under SQLite (test DB has no Postgres roles).
 */
return new class extends Migration
{
    /** @var list<string> */
    private const TABLES = [
        'silver.reports',
        'silver.spatial_features',
        'silver.seismic_surveys',
        'silver.raster_layers',
        'silver.answer_runs',
        'silver.geophysics_surveys',
        'silver.campaigns',
        'silver.review_queue',
        'gold.zone_statistics',
        'gold.element_correlations',
        'silver.projects',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        foreach (self::TABLES as $table) {
            DB::statement("GRANT DELETE ON {$table} TO georag_app");
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        foreach (self::TABLES as $table) {
            DB::statement("REVOKE DELETE ON {$table} FROM georag_app");
        }
    }
};
