<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Per-role statement_timeout / lock_timeout for the application role
 * (database audit 2026-10).
 *
 * docker-compose.yml carries `lock_timeout=3000` (and the pool-side code sets
 * `SET LOCAL statement_timeout` per request), but the RDS parameter group
 * cannot set them for the whole cluster without also capping the migration
 * role and pg_cron maintenance, so on AWS nothing bounded a runaway query or a
 * lock queue: one slow ingest holding a lock stalls every chat query behind it,
 * and a wedged query holds an RDS connection until someone notices.
 *
 *   ALTER ROLE georag_app SET statement_timeout = '300s'
 *   ALTER ROLE georag_app SET lock_timeout      = '10s'
 *
 * georag_app is the one role every application service connects as (Laravel,
 * FastAPI, hatchet-worker, martin's sibling reads), so the setting rides with
 * the role and needs no deploy-side change. Migrations run as `georag` and
 * pg_cron/partman as the RDS master, so neither is capped.
 *
 * NEVER LOOSEN. If the server already sets a stricter (non-zero) value --
 * compose's lock_timeout=3000 -- the role setting is skipped, because a
 * role-level setting overrides the server default and would turn 3 s into 10 s
 * in dev. The check reads pg_settings in the migrating session, which reflects
 * the cluster/database default.
 *
 * Guards: skipped when the role does not exist (fresh local cluster before the
 * role migration) and when the migrating role may not alter it (`ALTER ROLE ..
 * SET` needs CREATEROLE with ADMIN OPTION on the role, or superuser). On the
 * ECS migrate task `georag` is the RDS master and does hold that; the
 * "Migrations under production privileges" CI job runs as a CREATEROLE role
 * that did not create georag_app, so there it logs a NOTICE and moves on rather
 * than failing the chain. In that case run the statements by hand (see
 * deploy/aws/README.md).
 *
 * OPERATOR NOTE. Role settings apply to NEW sessions only. After this runs,
 * existing connections (Octane workers, FastAPI pools, hatchet-worker) keep
 * their old settings until they reconnect: restart the ECS services, or wait for
 * the next nightly stop/start. And a statement longer than 120 s as georag_app
 * -- a very large COPY, a first-time MV population -- is now cancelled; a job
 * that legitimately needs longer must `SET LOCAL statement_timeout` for itself.
 *
 * pgsql only.
 */
return new class extends Migration
{
    private const STATEMENT_TIMEOUT = '300s';

    private const LOCK_TIMEOUT = '10s';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $statement = self::STATEMENT_TIMEOUT;
        $lock = self::LOCK_TIMEOUT;

        DB::unprepared(<<<SQL
            DO \$do\$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    RETURN;
                END IF;

                BEGIN
                    IF (SELECT setting::bigint FROM pg_settings WHERE name = 'statement_timeout') = 0 THEN
                        ALTER ROLE georag_app SET statement_timeout = '{$statement}';
                    END IF;
                    IF (SELECT setting::bigint FROM pg_settings WHERE name = 'lock_timeout') = 0 THEN
                        ALTER ROLE georag_app SET lock_timeout = '{$lock}';
                    END IF;
                EXCEPTION WHEN insufficient_privilege THEN
                    RAISE NOTICE 'not permitted to ALTER ROLE georag_app; set statement_timeout / lock_timeout by hand';
                END;
            END
            \$do\$;
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::unprepared(<<<'SQL'
            DO $do$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    RETURN;
                END IF;

                BEGIN
                    ALTER ROLE georag_app RESET statement_timeout;
                    ALTER ROLE georag_app RESET lock_timeout;
                EXCEPTION WHEN insufficient_privilege THEN
                    RAISE NOTICE 'not permitted to ALTER ROLE georag_app; reset statement_timeout / lock_timeout by hand';
                END;
            END
            $do$;
        SQL);
    }
};
