<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function getConnection(): ?string
    {
        // Pin to the dedicated owner role only when the `pgsql_migrations`
        // connection is actually opted into (MIGRATE_DB_CONNECTION, set in
        // docker-compose.yml) — config/database.php documents this
        // connection as opt-in with `pgsql` as the "legacy behaviour"
        // fallback. The previous `!== 'sqlite'` check ignored that and
        // routed here unconditionally on any Postgres connection, which
        // breaks on CI/local test DBs: `pgsql_migrations` defaults to
        // host `postgresql` (the docker-compose service name), which
        // doesn't resolve outside that network.
        return config('database.migrations.connection') === 'pgsql_migrations' ? 'pgsql_migrations' : null;
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
