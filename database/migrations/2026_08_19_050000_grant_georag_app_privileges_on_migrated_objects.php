<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Gives `georag_app` the privileges it is supposed to have on objects created
 * by the migration chain, and makes future migrations grant them automatically.
 *
 * Migrations run as the `georag` owner (the `pgsql_migrations` connection).
 * Every table they create is therefore owned by `georag`, and `georag_app` —
 * the role FastAPI, Hatchet workers and Laravel actually connect as — gets
 * nothing unless a grant says otherwise.
 *
 * On a local cluster that grant comes from `docker/postgresql/init/init-roles.sql`
 * plus `database/raw/phase1/10-georag-app-role.sql`, which set ALTER DEFAULT
 * PRIVILEGES with `georag` as grantor. Neither file is part of the migration
 * chain, so the Azure server never received them, and the objects that only
 * exist there fail at runtime. Observed nightly in production:
 *
 *   retention_sweep               permission denied for table ingest_progress
 *   nightly_ingestion_integrity   permission denied for sequence mv_refresh_log_id_seq
 *
 * Both tables ARE in the migration chain (2026_05_24_230000, 2026_05_25_020546);
 * only the privileges were missing. Same shape as silver.collars.geom_4326 and
 * the workflow.* functions: applied by hand locally, never declared.
 *
 * Two halves, because ALTER DEFAULT PRIVILEGES is not retroactive:
 *   1. a catch-up GRANT over existing tables/sequences
 *   2. ALTER DEFAULT PRIVILEGES FOR ROLE georag, so the next migration's
 *      tables are covered without anyone remembering to do this again
 *
 * Privileges mirror the local cluster exactly (read out of pg_default_acl) so
 * the two environments converge rather than drift further apart. This does NOT
 * weaken tenancy: every one of these tables is under FORCE ROW LEVEL SECURITY,
 * and a grant does not bypass an RLS policy.
 *
 * Only `georag_app` is touched. The reporting roles (georag_read, georag_write,
 * martin_readonly) are left alone — nothing has been observed failing for them,
 * and widening them is a separate decision.
 *
 * Idempotent: GRANT and ALTER DEFAULT PRIVILEGES are both repeatable.
 */
return new class extends Migration
{
    /**
     * schema => [table privileges, sequence privileges]
     *
     * @var array<string, array{0: string, 1: string}>
     */
    private const SCHEMA_PRIVILEGES = [
        'audit' => ['INSERT, SELECT, UPDATE', 'SELECT, USAGE, UPDATE'],
        'bronze' => ['INSERT, SELECT, UPDATE, DELETE, REFERENCES', 'SELECT, USAGE, UPDATE'],
        'gold' => ['INSERT, SELECT, UPDATE, DELETE, REFERENCES', 'SELECT, USAGE, UPDATE'],
        'outbox' => ['INSERT, SELECT, UPDATE', 'SELECT, USAGE, UPDATE'],
        'public' => ['INSERT, SELECT, UPDATE, DELETE', 'SELECT, USAGE, UPDATE'],
        'public_geo' => ['INSERT, SELECT, UPDATE', 'SELECT, USAGE, UPDATE'],
        'silver' => ['INSERT, SELECT, UPDATE, DELETE, REFERENCES', 'SELECT, USAGE, UPDATE'],
        'usage' => ['INSERT, SELECT, UPDATE', 'SELECT, USAGE, UPDATE'],
        'workflow' => ['INSERT, SELECT, UPDATE', 'SELECT, USAGE, UPDATE'],
        'workspace' => ['INSERT, SELECT, UPDATE', 'SELECT, USAGE, UPDATE'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->roleExists('georag_app')) {
            // Local clusters that never provisioned the app role. Nothing to
            // grant to, and inventing the role here would hide a broken init.
            return;
        }

        foreach (self::SCHEMA_PRIVILEGES as $schema => [$tablePrivileges, $sequencePrivileges]) {
            if (! $this->schemaExists($schema)) {
                continue;
            }

            DB::statement(sprintf('GRANT USAGE ON SCHEMA %s TO georag_app', $schema));

            // 1. Catch up everything that already exists.
            DB::statement(sprintf(
                'GRANT %s ON ALL TABLES IN SCHEMA %s TO georag_app',
                $tablePrivileges,
                $schema,
            ));
            DB::statement(sprintf(
                'GRANT %s ON ALL SEQUENCES IN SCHEMA %s TO georag_app',
                $sequencePrivileges,
                $schema,
            ));

            // 2. Cover everything a future migration creates. FOR ROLE georag
            //    is the important part — default privileges apply per grantor,
            //    and migrations create their objects as georag.
            DB::statement(sprintf(
                'ALTER DEFAULT PRIVILEGES FOR ROLE georag IN SCHEMA %s GRANT %s ON TABLES TO georag_app',
                $schema,
                $tablePrivileges,
            ));
            DB::statement(sprintf(
                'ALTER DEFAULT PRIVILEGES FOR ROLE georag IN SCHEMA %s GRANT %s ON SEQUENCES TO georag_app',
                $schema,
                $sequencePrivileges,
            ));
        }
    }

    /**
     * Deliberately a no-op.
     *
     * Revoking would break the running application, and these privileges are
     * what every other environment already has. There is nothing to roll back
     * to that is not simply "broken".
     */
    public function down(): void
    {
        //
    }

    private function schemaExists(string $schema): bool
    {
        return DB::selectOne(
            'SELECT 1 AS present FROM information_schema.schemata WHERE schema_name = ?',
            [$schema],
        ) !== null;
    }

    private function roleExists(string $role): bool
    {
        return DB::selectOne(
            'SELECT 1 AS present FROM pg_roles WHERE rolname = ?',
            [$role],
        ) !== null;
    }
};
