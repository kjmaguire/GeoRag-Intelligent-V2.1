<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Close the bronze-layer tenancy gap surfaced by the Foundry Lakehouse
 * audit on 2026-05-25: bronze.source_files, bronze.ingest_manifest and
 * bronze.provenance were rendering global cross-workspace counts because
 * RLS was never enabled and (for two of them) no workspace_id column
 * existed at all.
 *
 * Three tables, three subtly different treatments:
 *
 *   1. bronze.source_files
 *      Already has workspace_id NOT NULL. Just needs ENABLE ROW LEVEL
 *      SECURITY + the canonical workspace_isolation policy.
 *
 *   2. bronze.ingest_manifest
 *      Bulk-import manifest for ZIP-based uploads — no tenancy column.
 *      Adds workspace_id UUID NULL (no FK; bronze tables stay loose).
 *      No reliable backfill source exists for legacy rows (outer_zip_path
 *      doesn't match source_files.seaweedfs_key — different upload paths).
 *      Existing rows stay NULL.
 *
 *   3. bronze.provenance
 *      Per-row lineage for every silver INSERT — no tenancy column.
 *      Adds workspace_id UUID NULL, backfills opportunistically by
 *      joining target_id -> silver.<target_table>.workspace_id for the
 *      target tables we know the FK shape of (collars, samples,
 *      lithology_logs, reports, spatial_features, raster_layers).
 *      Rows pointing at other targets (or already-deleted targets)
 *      stay NULL.
 *
 * Policy shape — matches the canonical pattern from
 * 2026_05_19_180100_enable_rls_on_uncovered_workspace_tables and
 * audit.query_audit_log:
 *
 *     USING (
 *       NULLIF(current_setting('app.workspace_id', true), '') IS NULL
 *       OR workspace_id IS NULL                              -- legacy rows
 *       OR workspace_id = NULLIF(current_setting(...), '')::uuid
 *     )
 *
 * The `workspace_id IS NULL` exemption keeps legacy + manifest rows
 * readable until the writers are updated to populate workspace_id (a
 * follow-up tracked separately). Without it this migration would
 * black-hole tens of thousands of existing rows from every UI.
 *
 * Writer follow-up (NOT in this migration):
 *   - src/dagster/georag_dagster/assets/silver*.py — 5 INSERT sites
 *     that need to pass the asset's workspace_id into the provenance
 *     INSERT statement.
 *   - bronze.ingest_manifest writers (the ZIP-scan pipeline) need to
 *     either take a workspace_id parameter or be deprecated in favor
 *     of bronze.source_files.
 * Once both writers populate workspace_id consistently, a follow-up
 * migration can drop the `IS NULL` exemption and add NOT NULL.
 *
 * SQLite (test DB) does not support RLS — gated on Postgres. The
 * column-add half also runs in SQLite for schema parity.
 */
return new class extends Migration
{
    /**
     * Tables that store provenance.target_id values whose target table
     * carries a workspace_id column. Used to opportunistically backfill
     * bronze.provenance.workspace_id without writing a row-by-row
     * stored procedure.
     *
     * @var array<int, array{schema: string, table: string, pk: string}>
     */
    private const PROVENANCE_BACKFILL_TARGETS = [
        ['schema' => 'silver', 'table' => 'collars',          'pk' => 'collar_id'],
        ['schema' => 'silver', 'table' => 'samples',          'pk' => 'sample_id'],
        ['schema' => 'silver', 'table' => 'lithology_logs',   'pk' => 'log_id'],
        ['schema' => 'silver', 'table' => 'reports',          'pk' => 'report_id'],
        ['schema' => 'silver', 'table' => 'spatial_features', 'pk' => 'feature_id'],
        ['schema' => 'silver', 'table' => 'raster_layers',    'pk' => 'raster_id'],
    ];

    public function up(): void
    {
        // The bronze schema itself is Postgres-only (created via raw SQL
        // in 2026_04_18_130000_create_bronze_provenance_table and its
        // siblings, all of which assume Postgres). Skipping the entire
        // migration in SQLite keeps test runs working — the test DB has
        // no bronze.* tables to alter.
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // ── 1. Add workspace_id columns where missing ────────────────
        DB::statement('ALTER TABLE bronze.ingest_manifest ADD COLUMN IF NOT EXISTS workspace_id UUID NULL');
        DB::statement('ALTER TABLE bronze.provenance      ADD COLUMN IF NOT EXISTS workspace_id UUID NULL');

        // Partial btree indexes so the workspace filter inside RLS
        // USING clauses doesn't sequential-scan once the tables grow.
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_ingest_manifest_workspace_idx ON bronze.ingest_manifest (workspace_id) WHERE workspace_id IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_provenance_workspace_idx      ON bronze.provenance      (workspace_id) WHERE workspace_id IS NOT NULL');

        // ── 2. Best-effort backfill on bronze.provenance ─────────────
        // Each target table contributes whatever rows it can match.
        // bronze.ingest_manifest is intentionally NOT backfilled (no
        // reliable join — see class doc).
        foreach (self::PROVENANCE_BACKFILL_TARGETS as $t) {
            $exists = DB::table('information_schema.tables')
                ->where('table_schema', $t['schema'])
                ->where('table_name', $t['table'])
                ->exists();
            if (! $exists) {
                continue;
            }
            $hasWorkspace = DB::table('information_schema.columns')
                ->where('table_schema', $t['schema'])
                ->where('table_name', $t['table'])
                ->where('column_name', 'workspace_id')
                ->exists();
            if (! $hasWorkspace) {
                continue;
            }
            $qualified = $t['schema'].'.'.$t['table'];
            DB::statement(<<<SQL
                UPDATE bronze.provenance bp
                SET workspace_id = tgt.workspace_id
                FROM {$qualified} AS tgt
                WHERE bp.workspace_id IS NULL
                  AND bp.target_schema = '{$t['schema']}'
                  AND bp.target_table  = '{$t['table']}'
                  AND bp.target_id     = tgt.{$t['pk']}
            SQL);
        }

        // ── 3. Enable RLS + policies ─────────────────────────────────
        // bronze.source_files — workspace_id is NOT NULL on this table,
        // so no IS NULL exemption is needed.
        DB::statement('ALTER TABLE bronze.source_files ENABLE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY bronze_source_files_workspace_isolation ON bronze.source_files
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        // bronze.ingest_manifest — IS NULL exemption keeps legacy rows
        // and any pre-writer-update inserts visible.
        DB::statement('ALTER TABLE bronze.ingest_manifest ENABLE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY bronze_ingest_manifest_workspace_isolation ON bronze.ingest_manifest
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        // bronze.provenance — same shape; backfill above tagged what it
        // could, the rest stay NULL until writers populate.
        DB::statement('ALTER TABLE bronze.provenance ENABLE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY bronze_provenance_workspace_isolation ON bronze.provenance
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach ([
            ['bronze.source_files',    'bronze_source_files_workspace_isolation'],
            ['bronze.ingest_manifest', 'bronze_ingest_manifest_workspace_isolation'],
            ['bronze.provenance',      'bronze_provenance_workspace_isolation'],
        ] as [$tbl, $policy]) {
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$tbl}");
            DB::statement("ALTER TABLE {$tbl} DISABLE ROW LEVEL SECURITY");
        }

        DB::statement('DROP INDEX IF EXISTS bronze.bronze_ingest_manifest_workspace_idx');
        DB::statement('DROP INDEX IF EXISTS bronze.bronze_provenance_workspace_idx');
        DB::statement('ALTER TABLE bronze.ingest_manifest DROP COLUMN IF EXISTS workspace_id');
        DB::statement('ALTER TABLE bronze.provenance      DROP COLUMN IF EXISTS workspace_id');
    }
};
