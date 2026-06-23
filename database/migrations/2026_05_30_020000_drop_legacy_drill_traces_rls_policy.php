<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function getConnection(): ?string
    {
        // Pin to the dedicated owner role only on PostgreSQL, where DROP
        // POLICY requires table-owner privileges. Under the SQLite test
        // connection there is no `pgsql_migrations` server to reach (it
        // would resolve to host `postgresql` / database `:memory:` and hang
        // on a TCP timeout); fall back to the default connection so the
        // SQLite compatibility hook can no-op the DROP POLICY statement.
        return config('database.default') === 'sqlite' ? null : 'pgsql_migrations';
    }

    public function up(): void
    {
        DB::statement('DROP POLICY IF EXISTS drill_traces_workspace_isolation ON silver.drill_traces');
    }

    public function down(): void
    {
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_policies
                    WHERE schemaname = 'silver'
                      AND tablename  = 'drill_traces'
                      AND policyname = 'drill_traces_workspace_isolation'
                ) THEN
                    CREATE POLICY drill_traces_workspace_isolation
                        ON silver.drill_traces
                        USING (workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid);
                END IF;
            END
            $$
        SQL);
    }
};
