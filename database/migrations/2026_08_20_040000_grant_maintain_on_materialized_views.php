<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Lets `georag_app` read and REFRESH materialized views.
 *
 * `2026_08_19_050000_grant_georag_app_privileges_on_migrated_objects` fixed the
 * table and sequence grants, and the 2026-08-20 06:29 UTC migrate run cleared
 * every privilege error in production except one, which is still firing:
 *
 *   2026-08-20T15:50:22Z fastapi-cc  triggered_by=ingestion
 *   mv_refresh: failed view=silver.mv_collar_summary
 *     err=InsufficientPrivilegeError: permission denied for materialized view
 *
 * Two separate causes, both fixed here.
 *
 * ── 1. Materialized views were never in scope ───────────────────────────────
 *
 * That migration's per-object grant loop filters on
 * `relkind IN ('r','p','v','f')`. Materialized views are relkind 'm', so they
 * matched nothing and received no grants at all — not even SELECT. The
 * relkind list was written to mirror what `GRANT ... ON ALL TABLES` covers,
 * and `ON ALL TABLES` does include matviews, so the omission is a
 * transcription slip rather than a deliberate exclusion.
 *
 * ── 2. REFRESH needs MAINTAIN, which no grant was asking for ────────────────
 *
 * SELECT alone would still not fix this. On PostgreSQL 17+ (both clusters run
 * 18.3) `REFRESH MATERIALIZED VIEW` is gated by the MAINTAIN privilege rather
 * than an ownership test — which is why the server says "permission denied
 * for materialized view" (an ACL failure) and not "must be owner of".
 * MAINTAIN is not implied by INSERT/SELECT/UPDATE/DELETE/REFERENCES, so it
 * has to be granted explicitly.
 *
 * ── Why local never caught this ─────────────────────────────────────────────
 *
 * This is the part worth remembering. Locally the application connects as
 * `POSTGRES_USER=georag` — the role that *owns* every one of these objects, so
 * it holds MAINTAIN implicitly and no privilege check can ever fail. On Azure
 * `POSTGRES_USER=georag_app`, the least-privilege role. The local cluster
 * therefore cannot reproduce any grant bug at all: this whole class of defect
 * is invisible until it reaches production. Worth fixing separately by
 * pointing local compose at georag_app.
 *
 * Scope is deliberately narrow: MAINTAIN is granted on materialized views
 * only, not on ordinary tables. On tables it would additionally confer VACUUM,
 * ANALYZE, CLUSTER, REINDEX and REFRESH rights the application has never asked
 * for. Note the consequence — a materialized view created by a *future*
 * migration will not be covered, because ALTER DEFAULT PRIVILEGES is not used
 * here. Adding MAINTAIN to the existing `ON TABLES` default privileges would
 * cover them, at the cost of that wider table-level grant; that is a privilege
 * decision rather than a bug fix, so it is left out of this migration.
 *
 * Idempotent: GRANT is repeatable, and objects the migrating role cannot grant
 * on are skipped with a NOTICE rather than aborting the statement — the same
 * failure mode that broke the 2026-08-19 deploy.
 */
return new class extends Migration
{
    /**
     * Schemas searched for materialized views, matching the schema list in
     * 2026_08_19_050000. `silver` is the only one holding a matview today
     * (silver.mv_collar_summary); the rest are covered so a matview added to
     * any of them is picked up when this migration runs.
     */
    private const SCHEMAS = [
        'audit', 'bronze', 'gold', 'outbox', 'public',
        'public_geo', 'silver', 'usage', 'workflow', 'workspace',
    ];

    /**
     * SELECT so the refresh helper can count rows before and after; MAINTAIN
     * so REFRESH MATERIALIZED VIEW itself is permitted.
     */
    private const PRIVILEGES = 'SELECT, MAINTAIN';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (! $this->supportsMaintain()) {
            // PostgreSQL 16 and earlier gate REFRESH on ownership and have no
            // MAINTAIN privilege to grant. Both clusters run 18.3, so this is
            // a guard for older local images rather than an expected path —
            // granting SELECT there is still correct and still an improvement.
            $this->grantOnMatviews('SELECT');

            return;
        }

        $this->grantOnMatviews(self::PRIVILEGES);
    }

    /**
     * Deliberately a no-op.
     *
     * Revoking would restore a state where per-ingestion MV refresh fails
     * silently at WARNING level. There is nothing to roll back to that is not
     * simply the bug.
     */
    public function down(): void
    {
        //
    }

    /**
     * Grant per object rather than via `ON ALL TABLES`.
     *
     * `ON ALL TABLES` is atomic and needs grant option on every relation in the
     * schema, so a single object owned elsewhere aborts the whole statement.
     * That is exactly what failed on 2026-08-19 with
     * audit.integration_credentials_audit, which is deliberately owned by a
     * more privileged role. Looping and skipping what we cannot grant on keeps
     * the intent without asserting authority over objects the migration chain
     * did not create.
     */
    private function grantOnMatviews(string $privileges): void
    {
        foreach (self::SCHEMAS as $schema) {
            if (! $this->schemaExists($schema)) {
                continue;
            }

            DB::statement(<<<SQL
                DO \$\$
                DECLARE
                    rec record;
                    granted int := 0;
                    skipped int := 0;
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                        RETURN;
                    END IF;

                    FOR rec IN
                        SELECT c.oid::regclass AS qualified,
                               pg_has_role(current_user, c.relowner, 'USAGE') AS grantable
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = '{$schema}'
                          AND c.relkind = 'm'
                    LOOP
                        IF rec.grantable THEN
                            EXECUTE format(
                                'GRANT {$privileges} ON TABLE %s TO georag_app',
                                rec.qualified
                            );
                            granted := granted + 1;
                        ELSE
                            skipped := skipped + 1;
                            RAISE NOTICE
                                'skipping % — not owned by %, cannot grant',
                                rec.qualified, current_user;
                        END IF;
                    END LOOP;

                    IF granted > 0 THEN
                        RAISE NOTICE
                            '{$schema}: granted {$privileges} on % materialized view(s) to georag_app',
                            granted;
                    END IF;

                    IF skipped > 0 THEN
                        RAISE NOTICE
                            '{$schema}: materialized view grants skipped for % object(s) not owned by %',
                            skipped, current_user;
                    END IF;
                END
                \$\$;
            SQL);
        }
    }

    /**
     * MAINTAIN landed in PostgreSQL 17. Probing the privilege name directly is
     * more honest than parsing server_version_num, because what actually
     * matters is whether this server accepts it in a GRANT.
     */
    private function supportsMaintain(): bool
    {
        try {
            DB::selectOne("SELECT has_table_privilege(current_user, 'pg_class', 'MAINTAIN') AS ok");

            return true;
        } catch (Throwable) {
            return false;
        }
    }

    private function schemaExists(string $schema): bool
    {
        return DB::selectOne(
            'SELECT 1 AS present FROM information_schema.schemata WHERE schema_name = ?',
            [$schema],
        ) !== null;
    }
};
