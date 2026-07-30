<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB parity fix — core lakehouse schema namespaces, role objects,
 * and extensions.
 *
 * Production databases get bronze/silver/gold/index and the postgis/
 * pg_trgm/uuid-ossp extensions from docker/postgresql/init/init-postgis.sql,
 * and the georag_read/write/audit roles from
 * docker/postgresql/init/init-roles.sql — both are docker-entrypoint-
 * initdb.d scripts that only run once on a fresh Postgres data directory.
 * CI's Laravel job uses an ephemeral `postgis/postgis` GitHub Actions
 * service container with no init scripts mounted (the image's own initdb
 * hook installs `postgis`/`postgis_topology` but not `pg_trgm` or
 * `uuid-ossp`), so on a from-scratch test DB:
 *   - the very next migration (create_projects_table) fails with
 *     "schema silver does not exist" creating `silver.projects`,
 *   - later migrations (e.g. move_query_audit_log_to_audit_schema) fail
 *     with "role georag_read does not exist" on their GRANT statements, and
 *   - still later migrations (e.g. create_silver_ingest_ocr_results) fail
 *     with 'operator class "gin_trgm_ops" does not exist" on a GIN/trigram
 *     index because pg_trgm was never installed.
 * `pg_stat_statements` is deliberately NOT created here even though
 * init-postgis.sql installs it — it requires `shared_preload_libraries`
 * set at server start, which CI's plain service container doesn't set,
 * and no migration or test depends on it.
 *
 * Same pattern as the audit/workflow/usage `provision_*_for_test_db.php`
 * migrations: mirror the schema/role/extension half of the raw-SQL/init-
 * script bootstrap into a migration so `php artisan migrate` alone can
 * build a working test DB. Roles are created NOLOGIN with no grants of
 * their own — tests connect as the migration user directly, so downstream
 * migrations' GRANT statements only need the role objects to exist, not
 * a full privilege replica of init-roles.sql. `CREATE SCHEMA/ROLE/
 * EXTENSION IF NOT EXISTS` is a no-op anywhere it already exists
 * (production, local dev via docker-compose).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('CREATE SCHEMA IF NOT EXISTS bronze');
        DB::statement('CREATE SCHEMA IF NOT EXISTS silver');
        DB::statement('CREATE SCHEMA IF NOT EXISTS gold');
        DB::statement('CREATE SCHEMA IF NOT EXISTS index');

        DB::statement('CREATE EXTENSION IF NOT EXISTS postgis');
        DB::statement('CREATE EXTENSION IF NOT EXISTS postgis_topology');
        DB::statement('CREATE EXTENSION IF NOT EXISTS pg_trgm');
        DB::statement('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"');

        DB::statement(<<<'SQL'
            DO $$ BEGIN
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'georag_app') THEN
                    CREATE ROLE georag_app NOLOGIN;
                END IF;
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'georag_read') THEN
                    CREATE ROLE georag_read NOLOGIN;
                END IF;
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'georag_write') THEN
                    CREATE ROLE georag_write NOLOGIN;
                END IF;
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'georag_audit') THEN
                    CREATE ROLE georag_audit NOLOGIN;
                END IF;
            END $$
        SQL);
    }

    public function down(): void
    {
        // No-op: dropping these schemas would cascade through every
        // table every other migration creates in them. Schema cleanup
        // isn't this migration's responsibility to reverse.
    }
};
