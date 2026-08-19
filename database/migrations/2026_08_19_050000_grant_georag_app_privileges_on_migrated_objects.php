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
            //
            // NOT `GRANT ... ON ALL TABLES IN SCHEMA`. That form is atomic and
            // requires the grantor to hold grant option on EVERY relation in
            // the schema — one object it doesn't own aborts the whole
            // statement. That is exactly what broke the 2026-08-19 deploy:
            //
            //   SQLSTATE[42501]: permission denied for table
            //   integration_credentials_audit
            //   (SQL: GRANT INSERT, SELECT, UPDATE ON ALL TABLES IN SCHEMA
            //    audit TO georag_app)
            //
            // audit.integration_credentials_audit is deliberately owned by a
            // more privileged role — it is admin-only, gated by RBAC rather
            // than RLS (see database/raw/phase0/95-rls-policies.sql's "Tables
            // that DO NOT get RLS" list). georag must not own it, so this
            // migration must not try to grant on it.
            //
            // Granting per-relation and skipping what we cannot grant on
            // keeps the intent (georag_app can reach the objects migrations
            // create) without asserting authority over objects migrations did
            // not create. Skips are RAISE NOTICE'd rather than swallowed
            // silently so a genuinely-missing grant is still discoverable in
            // the migrate job log.
            $this->grantPerObject($schema, $tablePrivileges, isSequence: false);
            $this->grantPerObject($schema, $sequencePrivileges, isSequence: true);

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

    /**
     * GRANT the given privileges on every relation in $schema that the
     * current role is actually allowed to grant on, skipping the rest.
     *
     * "Allowed to grant on" is tested as ownership —
     * pg_has_role(current_user, relowner, 'USAGE') — which is true when the
     * current role owns the object or is a member of the owning role. That
     * is the condition Postgres itself applies for the ordinary case, and it
     * is what distinguishes migration-created objects (owned by georag) from
     * out-of-band admin objects that are deliberately owned elsewhere.
     *
     * relkind filter mirrors what `ON ALL TABLES` covers: ordinary tables
     * ('r'), partitioned tables ('p'), views ('v') and foreign tables ('f').
     * Sequences ('S') are handled by the same helper via $isSequence so the
     * ownership rule cannot drift between the two paths.
     */
    private function grantPerObject(string $schema, string $privileges, bool $isSequence): void
    {
        $relkinds = $isSequence ? "'S'" : "'r','p','v','f'";
        $objectWord = $isSequence ? 'SEQUENCE' : 'TABLE';

        DB::statement(<<<SQL
            DO \$\$
            DECLARE
                rec record;
                skipped int := 0;
            BEGIN
                FOR rec IN
                    SELECT c.oid::regclass AS qualified,
                           pg_has_role(current_user, c.relowner, 'USAGE') AS grantable
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = '{$schema}'
                      AND c.relkind IN ({$relkinds})
                LOOP
                    IF rec.grantable THEN
                        EXECUTE format(
                            'GRANT {$privileges} ON {$objectWord} %s TO georag_app',
                            rec.qualified
                        );
                    ELSE
                        skipped := skipped + 1;
                        RAISE NOTICE
                            'skipping % — not owned by %, cannot grant',
                            rec.qualified, current_user;
                    END IF;
                END LOOP;

                IF skipped > 0 THEN
                    RAISE NOTICE
                        '{$schema}: {$objectWord} grants skipped for % object(s) not owned by %',
                        skipped, current_user;
                END IF;
            END
            \$\$;
        SQL);
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
