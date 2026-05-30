<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function getConnection(): string
    {
        return 'pgsql_migrations';
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
