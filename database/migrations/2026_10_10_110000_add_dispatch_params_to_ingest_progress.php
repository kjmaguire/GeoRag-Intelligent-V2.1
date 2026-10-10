<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * silver.ingest_progress.dispatch_params -- what the uploader told the ingest
 * workflow, kept so a RE-dispatch can say the same thing (2026-10 Hatchet
 * audit, finding 5).
 *
 * stale_run_detector and nightly_ingestion_integrity rebuild a workflow input
 * from the progress row when they recover a run. The row carries identity
 * (workspace, project, key) and nothing the uploader DECLARED, so a recovered
 * ingest_tabular run dropped source_epsg and column_map and fell back to
 * `epsg = input.source_epsg or DEFAULT_SOURCE_EPSG` (EPSG:32613, UTM 13N): a
 * collar file the geologist had told us was EPSG:26904 was re-placed in the
 * wrong zone, silently, by the very sweep meant to rescue it. ingest_spatial
 * lost source_crs_wkt and feature_type the same way, ingest_geophysics its
 * source_epsg, ingest_well_logs its hole_id, and a zip its source_epsg for
 * every member.
 *
 * The column holds only the declared fields of the workflow input (the
 * whitelist is _progress.DISPATCH_PARAM_FIELDS), as a JSON object. The three
 * states are deliberate:
 *
 *   NULL  not recorded -- a row from before this column, or one written by a
 *         path that does not record it. For a workflow whose input has
 *         declared fields that is NOT the same as "none were declared", so
 *         the sweeps decline to re-dispatch it (a timed_out row with a
 *         warning) rather than guess;
 *   {}    recorded: the upload declared nothing, defaults are what it wanted;
 *   {...} replayed verbatim into the recovery run (and carried onto the
 *         recovery row, so a second-level recovery still has it).
 *
 * Nullable, no default: ADD COLUMN of that shape is a catalog-only change, so
 * there is no table rewrite and no lock beyond the brief ACCESS EXCLUSIVE of
 * the ALTER. Nothing indexes it. RLS is unaffected (it is row-level), and
 * georag_app's table-level SELECT/INSERT/UPDATE already covers a new column.
 *
 * down() drops the column (the data is a copy of what the trigger endpoint
 * was sent; nothing else reads it).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (! $this->tableExists()) {
            return;
        }

        DB::statement(
            'ALTER TABLE silver.ingest_progress ADD COLUMN IF NOT EXISTS dispatch_params jsonb NULL',
        );
        DB::statement(<<<'SQL'
COMMENT ON COLUMN silver.ingest_progress.dispatch_params IS
    'The uploader-declared fields of the ingest workflow input (source_epsg, column_map, ...), replayed when a sweep re-dispatches this run. NULL = not recorded (do not guess on recovery); {} = nothing was declared.'
SQL);
    }

    public function down(): void
    {
        if (! $this->tableExists()) {
            return;
        }

        DB::statement('ALTER TABLE silver.ingest_progress DROP COLUMN IF EXISTS dispatch_params');
    }

    private function tableExists(): bool
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return false;
        }

        return (bool) (DB::selectOne(
            "SELECT to_regclass('silver.ingest_progress') IS NOT NULL AS present",
        )->present ?? false);
    }
};
