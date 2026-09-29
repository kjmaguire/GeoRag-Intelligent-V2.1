<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * LAS files that arrived before their collar: kept, and attached later.
 *
 * WHY THIS TABLE EXISTS
 *   A LAS well log attaches curves to a collar. When the collar has not been
 *   uploaded yet, and the LAS header carries no coordinates of its own, there
 *   is nowhere honest to put the curves: silver.collars.easting / northing are
 *   NOT NULL and inventing a location is the one thing ingestion must not do.
 *   The file used to be refused and its curves lost, until the geologist
 *   noticed and re-uploaded it.
 *
 *   Now the file stays in bronze and this table records that it is waiting:
 *   which well, under which key, for which project. When collars are written
 *   for the project (the end of an ingest_tabular run that wrote collars, and
 *   the ZIP archive's dependent phase) `services/ingest/las_pending.py` looks
 *   for pending wells whose hole id (exact, then canonical) now has a collar
 *   and ingests them.
 *
 *   silver.ingest_progress was considered and does not fit: it is one row per
 *   upload keyed by (workspace_id, minio_key) with a stage state machine the
 *   stale-run sweep will time out, and "which pending wells match these hole
 *   ids" is a query it has no columns for.
 *
 * IDEMPOTENCY
 *   UNIQUE (project_id, bronze_key): re-recording the same waiting file
 *   updates the row instead of adding a second. `status` is the claim: a row
 *   moves pending -> attaching -> attached, only ever by a conditional UPDATE,
 *   so two workers cannot both ingest it, and an attached row is never
 *   picked again. A worker that dies mid-attach leaves 'attaching', which the
 *   attach query reclaims after 30 minutes. The curve writer itself is
 *   ON CONFLICT (collar_id, curve_name) DO UPDATE, so even a repeat attach
 *   replaces rather than duplicates.
 *
 * RLS
 *   Fail-closed shape, same as silver.attribute_tables.
 */
return new class extends Migration
{
    private const POLICY = 'las_pending_collar_workspace_isolation';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.las_pending_collar (
                pending_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id        uuid NOT NULL,
                project_id          uuid NOT NULL,
                hole_id             text NOT NULL,
                hole_id_canonical   text,
                bronze_key          text NOT NULL,
                source_name         text NOT NULL,
                status              text NOT NULL DEFAULT 'pending',
                collar_id           uuid,
                attempts            integer NOT NULL DEFAULT 0,
                last_error          text,
                created_at          timestamptz NOT NULL DEFAULT now(),
                updated_at          timestamptz NOT NULL DEFAULT now(),
                attached_at         timestamptz,

                CONSTRAINT chk_las_pending_collar_status
                    CHECK (status IN ('pending', 'attaching', 'attached')),
                CONSTRAINT uq_las_pending_collar_file
                    UNIQUE (project_id, bronze_key),

                CONSTRAINT fk_las_pending_collar_workspace
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,

                CONSTRAINT fk_las_pending_collar_project
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id)
                    ON DELETE CASCADE
            )
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_las_pending_collar_project_status
             ON silver.las_pending_collar (project_id, status)',
        );

        DB::statement("COMMENT ON TABLE silver.las_pending_collar IS
            'LAS well logs kept in bronze because their collar was not in the project yet. Attached automatically when a collar with the same hole id (exact, then canonical) is written; see app/services/ingest/las_pending.py.'");

        DB::statement('GRANT SELECT, INSERT, UPDATE ON silver.las_pending_collar TO georag_app');

        DB::statement('ALTER TABLE silver.las_pending_collar ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.las_pending_collar FORCE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS '.self::POLICY.' ON silver.las_pending_collar');
        DB::statement(
            'CREATE POLICY '.self::POLICY.' ON silver.las_pending_collar'
            .' USING (workspace_id = NULLIF(current_setting(\'app.workspace_id\', true), \'\')::uuid)'
            .' WITH CHECK (workspace_id = NULLIF(current_setting(\'app.workspace_id\', true), \'\')::uuid)',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS silver.las_pending_collar CASCADE');
    }
};
