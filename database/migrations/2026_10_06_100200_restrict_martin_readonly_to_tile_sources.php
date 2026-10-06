<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Cut martin_readonly back to the relations Martin's tile sources read
 * (database review 2026-10; least privilege on the Martin credential).
 *
 * ## The defect
 *
 * 2026_04_22_150000_grant_martin_readonly_select ran
 *
 *     ALTER DEFAULT PRIVILEGES IN SCHEMA silver     GRANT SELECT ON TABLES TO martin_readonly
 *     ALTER DEFAULT PRIVILEGES IN SCHEMA public_geo GRANT SELECT ON TABLES TO martin_readonly
 *
 * so every table any later migration created in `silver` became readable by the
 * tile server's role, and 2026_04_22_170000_extend_rls_workspace_coverage added
 * explicit SELECT on the evidence / answer tables so a pgTAP file could use the
 * role as a non-superuser test vehicle. On a migrated cluster martin_readonly
 * holds SELECT on ~90 silver tables, among them silver.answer_runs,
 * answer_citation_items, evidence_items, document_passages, query_traces,
 * claim_ledger, support_packets, message_feedback and qp_credentials -- none of
 * which any tile source touches.
 *
 * That role connects with MARTIN_DATABASE_URL, a credential held by the one
 * service that is reachable from the browser, and sets no app.workspace_id GUC.
 * Almost every tenant policy in this schema is the fail-open shape
 * (`NULLIF(current_setting('app.workspace_id', true), '') IS NULL OR ...`), so
 * with the GUC unset that role reads EVERY tenant's rows in every table it can
 * SELECT. Leak of the Martin credential, or any injection into a tile source,
 * was therefore a cross-tenant read of chat history, evidence text and QP
 * credentials rather than of map geometry.
 *
 * ## The fix
 *
 *   1. Revoke the two default-privilege grants, so a new table is private to
 *      its creator and georag_app until someone grants Martin access on purpose.
 *   2. Revoke SELECT on every silver table except the ones the tile functions
 *      and Martin's `tables:` config read, and on the two gold tables Martin
 *      never reads (gold.cross_section_panels stays).
 *   3. silver.significant_intersections_by_project is SECURITY DEFINER and takes
 *      its workspace_id from the caller's query string, so it reads gold rows
 *      with the owner's rights. EXECUTE on it was held by PUBLIC (no ACL set).
 *      It is now held by martin_readonly and georag_app only.
 *
 * The allow-list is the set of relations named in the bodies of the 18 Martin
 * function sources (docker/martin/martin.yaml) plus public.smdi_deposits and the
 * public_geo views, verified against the migrated schema:
 *
 *   silver: projects, collars, drill_traces, seismic_surveys,
 *           project_boundaries, geological_formations, historic_workings,
 *           geochemistry, spatial_features
 *   gold:   cross_section_panels
 *   (public_geo.jurisdictions and the eight v_pg_*_mvt views, and
 *    public.smdi_deposits, already hold only the grants they need and are not
 *    touched here.)
 *
 * silver.workspaces, which 2026_04_22_140000 granted inline, is read by no tile
 * source and is revoked with the rest.
 *
 * ## Consequences to know about
 *
 *   - A migration that adds a NEW Martin tile source must now GRANT SELECT on
 *     its source relation to martin_readonly explicitly (as the public_geo and
 *     silver MVT migrations already do for their functions).
 *   - database/tests/pgtap/11_rls_workspace_isolation.sql used martin_readonly
 *     as its non-superuser test role; it now uses georag_app, the role the
 *     application actually connects as, which is the more realistic scenario.
 *
 * ## Tenant isolation
 *
 * Strictly tighter: privileges are only removed. The workspace_id fence inside
 * each tile function (2026_09_16_120000) and every RLS policy are untouched, so
 * the three layers (RLS, query-level workspace_id, application) all still hold.
 *
 * Ownership: REVOKE needs grant-option on the object. Each statement runs in a
 * block that catches insufficient_privilege and logs a NOTICE, so a non-owner
 * migrating role (the CI "production privileges" job) does not abort; run the
 * NOTICE'd statements as the owner. pgsql only; a missing role or relation is
 * skipped. down() restores the previous (over-broad) grants verbatim.
 */
return new class extends Migration
{
    /** Silver relations the Martin tile functions read. */
    private const SILVER_ALLOWED = [
        'projects',
        'collars',
        'drill_traces',
        'seismic_surveys',
        'project_boundaries',
        'geological_formations',
        'historic_workings',
        'geochemistry',
        'spatial_features',
    ];

    /** Gold relations martin_readonly had that no tile source reads. */
    private const GOLD_REVOKED = [
        'structure_measurements_visual',
        'drillhole_intervals_visual',
    ];

    private const SIG_INTERSECTIONS = 'silver.significant_intersections_by_project(integer, integer, integer, json)';

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (DB::selectOne("SELECT 1 AS present FROM pg_roles WHERE rolname = 'martin_readonly'") === null) {
            return;
        }

        $allowed = implode(', ', array_map(static fn (string $t): string => "'{$t}'", self::SILVER_ALLOWED));
        $gold = implode(', ', array_map(static fn (string $t): string => "'{$t}'", self::GOLD_REVOKED));

        DB::unprepared(<<<SQL
            DO \$do\$
            DECLARE
                r record;
            BEGIN
                -- 1. Stop new tables inheriting a Martin grant.
                BEGIN
                    ALTER DEFAULT PRIVILEGES IN SCHEMA silver REVOKE SELECT ON TABLES FROM martin_readonly;
                    ALTER DEFAULT PRIVILEGES IN SCHEMA public_geo REVOKE SELECT ON TABLES FROM martin_readonly;
                EXCEPTION WHEN insufficient_privilege OR invalid_schema_name THEN
                    RAISE NOTICE 'martin_readonly: could not revoke default privileges; run ALTER DEFAULT PRIVILEGES ... REVOKE SELECT by hand';
                END;

                -- 2. Revoke every silver relation that is not a tile source.
                FOR r IN
                    SELECT format('%I.%I', n.nspname, c.relname) AS rel
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'silver'
                       AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
                       AND c.relname NOT IN ({$allowed})
                       AND has_table_privilege('martin_readonly', c.oid, 'SELECT')
                LOOP
                    BEGIN
                        EXECUTE 'REVOKE SELECT ON ' || r.rel || ' FROM martin_readonly';
                    EXCEPTION WHEN insufficient_privilege THEN
                        RAISE NOTICE 'martin_readonly: not permitted to revoke SELECT on %; run it by hand as the owner', r.rel;
                    END;
                END LOOP;

                FOR r IN
                    SELECT format('%I.%I', n.nspname, c.relname) AS rel
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'gold'
                       AND c.relname IN ({$gold})
                       AND has_table_privilege('martin_readonly', c.oid, 'SELECT')
                LOOP
                    BEGIN
                        EXECUTE 'REVOKE SELECT ON ' || r.rel || ' FROM martin_readonly';
                    EXCEPTION WHEN insufficient_privilege THEN
                        RAISE NOTICE 'martin_readonly: not permitted to revoke SELECT on %; run it by hand as the owner', r.rel;
                    END;
                END LOOP;
            END
            \$do\$;
        SQL);

        // 3. SECURITY DEFINER tile function: only the tile server (and the app
        // role, which pgTAP and operators use) may execute it.
        if (DB::selectOne('SELECT to_regprocedure(?) IS NOT NULL AS present', [self::SIG_INTERSECTIONS])->present) {
            $hasAppRole = DB::selectOne("SELECT 1 AS present FROM pg_roles WHERE rolname = 'georag_app'") !== null;
            $grantApp = $hasAppRole ? 'GRANT EXECUTE ON FUNCTION '.self::SIG_INTERSECTIONS.' TO georag_app;' : '';

            DB::unprepared(<<<SQL
                DO \$do\$
                BEGIN
                    GRANT EXECUTE ON FUNCTION {$this->sig()} TO martin_readonly;
                    {$grantApp}
                    REVOKE EXECUTE ON FUNCTION {$this->sig()} FROM PUBLIC;
                EXCEPTION WHEN insufficient_privilege THEN
                    RAISE NOTICE 'not permitted to change EXECUTE on {$this->sig()}; apply by hand as the owner';
                END
                \$do\$;
            SQL);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        if (DB::selectOne("SELECT 1 AS present FROM pg_roles WHERE rolname = 'martin_readonly'") === null) {
            return;
        }

        if (DB::selectOne('SELECT to_regprocedure(?) IS NOT NULL AS present', [self::SIG_INTERSECTIONS])->present) {
            DB::unprepared('GRANT EXECUTE ON FUNCTION '.self::SIG_INTERSECTIONS.' TO PUBLIC');
        }

        $gold = implode(', ', array_map(static fn (string $t): string => 'gold.'.$t, self::GOLD_REVOKED));
        DB::unprepared("GRANT SELECT ON {$gold} TO martin_readonly");

        DB::unprepared('GRANT SELECT ON ALL TABLES IN SCHEMA silver TO martin_readonly');
        DB::unprepared('ALTER DEFAULT PRIVILEGES IN SCHEMA silver GRANT SELECT ON TABLES TO martin_readonly');
        DB::unprepared('ALTER DEFAULT PRIVILEGES IN SCHEMA public_geo GRANT SELECT ON TABLES TO martin_readonly');
    }

    private function sig(): string
    {
        return self::SIG_INTERSECTIONS;
    }
};
