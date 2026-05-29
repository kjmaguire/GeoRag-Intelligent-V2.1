<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Final test-DB ↔ production parity reconciliation. Sibling to
 * 2026_05_25_175214_enable_rls_on_phase0_workspace_tables_reconciliation
 * — covers the four silver tables whose `workspace_id` column was
 * provisioned by 2026_05_25_184335_provision_silver_workspace_columns_for_test_db.
 *
 *   - silver.collars
 *   - silver.lithology_logs
 *   - silver.raster_layers
 *   - silver.samples
 *
 * In production each of these has RLS + a `<table>_workspace_isolation`
 * policy installed by `database/raw/phase0/96-rls-tenant-isolation-block1.sql`
 * (silver.collars + silver.samples) or by the broken-GUC-fix
 * migrations earlier today (silver.lithology_logs is added by phase0,
 * silver.raster_layers — same). We use the same no-op-when-covered
 * shape as 2026_05_25_175214: only install RLS + policy when neither
 * is already present.
 *
 * SQLite (test DB) does not support RLS — gated on Postgres.
 */
return new class extends Migration
{
    /** @var list<string> */
    private const TABLES = [
        'collars',
        'lithology_logs',
        'raster_layers',
        'samples',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::TABLES as $tbl) {
            $this->reconcile($tbl);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Drop only the policies WE installed (distinct _v2 suffix so
        // we never collide with production's existing canonical or
        // legacy-named policy).
        foreach (self::TABLES as $tbl) {
            $policy = "{$tbl}_workspace_isolation_v2";
            DB::statement("DROP POLICY IF EXISTS {$policy} ON silver.{$tbl}");
        }
    }

    private function reconcile(string $tbl): void
    {
        $tableExists = DB::table('information_schema.tables')
            ->where('table_schema', 'silver')
            ->where('table_name', $tbl)
            ->exists();
        if (! $tableExists) {
            return;
        }
        $hasWorkspaceCol = DB::table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', $tbl)
            ->where('column_name', 'workspace_id')
            ->exists();
        if (! $hasWorkspaceCol) {
            return;
        }

        // Already covered? No-op (the production path).
        $row = DB::selectOne(
            'SELECT c.relrowsecurity AS rls,
                    EXISTS (SELECT 1 FROM pg_policies p
                            WHERE p.schemaname = ? AND p.tablename = ?) AS has_policy
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = ? AND c.relname = ?',
            ['silver', $tbl, 'silver', $tbl],
        );
        if ($row && $row->rls && $row->has_policy) {
            return;
        }

        DB::statement("ALTER TABLE silver.{$tbl} ENABLE ROW LEVEL SECURITY");
        $policy = "{$tbl}_workspace_isolation_v2";
        DB::statement("DROP POLICY IF EXISTS {$policy} ON silver.{$tbl}");
        DB::statement(<<<SQL
            CREATE POLICY {$policy} ON silver.{$tbl}
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }
};
