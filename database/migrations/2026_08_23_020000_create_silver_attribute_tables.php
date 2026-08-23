<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * A landing table for standalone dBASE (`.dbf`) tables.
 *
 * WHY THIS TABLE EXISTS
 *   A GIS delivery is not only shapefiles. The RedStar delivery contains
 *   five `.dbf` files with no same-stem `.shp` beside them: legend tables,
 *   a survey-point register, a comment log. They are real data a geologist
 *   asked for by name, and until now they had nowhere to go — a standalone
 *   `.dbf` reaching the spatial path died with
 *   `AttributeError: 'DataFrame' object has no attribute 'crs'`, and the
 *   tabular path refused the extension outright.
 *
 *   They also match no geology schema. A dBASE table's columns are whatever
 *   the person who made it typed, so guessing them into silver.collars or
 *   silver.samples would be inventing structure. The rows land whole, as
 *   JSONB, with the file they came from recorded beside them.
 *
 * IDEMPOTENCY
 *   UNIQUE (project_id, source_file_sha256, source_layer, row_index) is what
 *   lets `app/hatchet_workflows/ingest_tabular.py` re-ingest the same file as
 *   a no-op instead of facing the replace-or-append choice the interval
 *   tables had to make (see _INTERVAL_TABLES there for why appending is the
 *   worse failure). Identical bytes hash the same, so the same rows update in
 *   place; a genuinely different export hashes differently and lands beside
 *   the old one rather than silently overwriting it.
 *
 *   The three key components are NOT NULL deliberately. A UNIQUE constraint
 *   over a NULL is not a constraint — PostgreSQL treats NULLs as distinct, so
 *   a nullable key would give the appearance of idempotency and none of the
 *   behaviour. `source_file` stays nullable because it is descriptive, not
 *   part of the identity.
 *
 * LINEAGE
 *   source_file / source_file_sha256 / source_layer are carried inline, the
 *   same shape silver.spatial_features uses and the reason ingest_tabular's
 *   entry in src/fastapi/tests/test_provenance_coverage.py describes this
 *   write as lineage-on-the-silver-row rather than a bronze.provenance gap.
 *
 * RLS
 *   Written directly in the current fail-closed shape (no `IS NULL OR`
 *   escape hatch, no chr(0) sentinel, `app.workspace_id` not the legacy
 *   `georag.*` GUC) — the three regressions
 *   tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php exists to catch. There
 *   is no reason to ship a new table already carrying documented debt.
 *
 * Test-DB parity: this IS the provision migration. It creates the table for
 * every pgsql environment including CI's test DB, so no separate
 * `*_provision_*_for_test_db` sibling is needed. Skipped on sqlite, where
 * neither the silver schema nor RLS exists.
 */
return new class extends Migration
{
    private const POLICY = 'attribute_tables_workspace_isolation';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.attribute_tables (
                attribute_row_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id        uuid NOT NULL,
                project_id          uuid NOT NULL,
                source_file         text,
                source_file_sha256  char(64) NOT NULL,
                source_layer        text NOT NULL,
                row_index           integer NOT NULL,
                attributes          jsonb NOT NULL DEFAULT '{}'::jsonb,
                created_at          timestamptz NOT NULL DEFAULT now(),
                updated_at          timestamptz NOT NULL DEFAULT now(),

                CONSTRAINT chk_attribute_tables_sha256_hex
                    CHECK (source_file_sha256 ~ '^[0-9a-f]{64}$'),
                CONSTRAINT chk_attribute_tables_row_index
                    CHECK (row_index >= 0),
                CONSTRAINT chk_attribute_tables_attributes_object
                    CHECK (jsonb_typeof(attributes) = 'object'),

                CONSTRAINT uq_attribute_tables_source_row
                    UNIQUE (project_id, source_file_sha256, source_layer, row_index),

                CONSTRAINT fk_attribute_tables_workspace
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,

                CONSTRAINT fk_attribute_tables_project
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id)
                    ON DELETE CASCADE
            )
        SQL);

        // The normal read is "everything this project got from this file".
        // The UNIQUE index already covers (project_id, source_file_sha256, …),
        // so this one exists for the workspace-scoped listing the RLS policy
        // and the Ingestion Runs surface both filter on.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_attribute_tables_workspace_project
             ON silver.attribute_tables (workspace_id, project_id)',
        );

        DB::statement("COMMENT ON TABLE silver.attribute_tables IS
            'Standalone dBASE (.dbf) tables — attribute rows with no geometry and no typed geology schema, landed whole as JSONB by app/hatchet_workflows/ingest_tabular.py. A .dbf WITH a same-stem .shp is a shapefile sidecar and belongs to ingest_spatial instead.'");

        DB::statement("COMMENT ON COLUMN silver.attribute_tables.source_layer IS
            'The dBASE layer name, which for a bare .dbf is the file stem. Part of the idempotency key so a multi-layer source cannot collide with itself.'");

        // UPDATE as well as INSERT: the writer's ON CONFLICT … DO UPDATE is
        // what makes a re-upload idempotent, and it needs the privilege.
        DB::statement('GRANT SELECT, INSERT, UPDATE ON silver.attribute_tables TO georag_app');

        DB::statement('ALTER TABLE silver.attribute_tables ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.attribute_tables FORCE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS '.self::POLICY.' ON silver.attribute_tables');
        DB::statement(
            'CREATE POLICY '.self::POLICY.' ON silver.attribute_tables'
            .' USING (workspace_id = NULLIF(current_setting(\'app.workspace_id\', true), \'\')::uuid)'
            .' WITH CHECK (workspace_id = NULLIF(current_setting(\'app.workspace_id\', true), \'\')::uuid)',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS silver.attribute_tables CASCADE');
    }
};
