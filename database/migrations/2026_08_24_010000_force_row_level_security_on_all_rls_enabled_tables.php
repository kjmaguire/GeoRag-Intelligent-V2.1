<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * FORCE ROW LEVEL SECURITY on EVERY table that has RLS enabled.
 *
 * `ENABLE ROW LEVEL SECURITY` does not apply to the table's OWNER; only
 * `FORCE` does. 2026_08_21_020000 closed that gap for the fourteen
 * reconciled tables from a hardcoded list, but a live census on 2026-08-24
 * found the pattern was never finished: 75 of 160 RLS-enabled tables in
 * production were still ENABLE-only, so the owner (`georag`, which
 * MIGRATE_DB_USERNAME connects as) bypassed their policies entirely
 * regardless of the policy text.
 *
 * ## Why catalog-driven rather than a table list
 *
 * The clusters this runs against disagree about which tables are in that
 * state (measured 2026-08-24: 53 on the integration cluster's georag_test,
 * 58 on the local dev DB, 75 in production) because table creation is split
 * across the migration chain, `database/raw/`, and per-environment history.
 * A static list correct for one cluster is wrong for the other two. The
 * invariant being installed is not "these N tables" but "no RLS-enabled
 * table exempts its owner" — so up() reads pg_class and closes whatever gap
 * the current cluster actually has, and
 * WorkspaceRlsCoverageTest::test_every_rls_enabled_table_is_forced pins the
 * invariant against regression from here on.
 *
 * ## Blast radius
 *
 * The change is confined to the OWNER role: `georag_app` was always subject
 * to the policies, and Postgres superusers / BYPASSRLS roles (local dev's
 * `georag`) remain exempt no matter what this sets. On the fail-open policy
 * shape that still covers most of the cluster (see
 * docs/architecture/fail-open-rls-posture-2026-08-21.md), an owner session
 * with no GUC bound still sees every row — the unset-GUC disjunct admits it
 * — so migrations-with-data, seeders, artisan commands and queue jobs behave
 * exactly as before. Only on the fail-closed tables does the owner now need
 * a bound GUC, and those were flipped precisely because nothing reads or
 * writes them unbound (2026_08_21_030000's docblock has the sweep).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;  // RLS is a Postgres feature
        }

        DB::statement(<<<'SQL'
            DO $$
            DECLARE
                tbl regclass;
            BEGIN
                FOR tbl IN
                    SELECT c.oid::regclass
                      FROM pg_class c
                      JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE c.relkind IN ('r', 'p')
                       AND c.relrowsecurity
                       AND NOT c.relforcerowsecurity
                       AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                     ORDER BY n.nspname, c.relname
                LOOP
                    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', tbl);
                END LOOP;
            END
            $$;
        SQL);
    }

    public function down(): void
    {
        // down() intentionally a no-op: the set of tables up() changed only
        // existed at run time (it is whatever the cluster had ENABLE-only at
        // that moment), so a faithful reverse would need state this migration
        // deliberately does not keep. More to the point, "the owner silently
        // bypasses tenant isolation on an unknowable subset of tables" is not
        // a state a rollback should restore. If a specific table genuinely
        // needs the owner exemption back, issue a targeted
        // `ALTER TABLE ... NO FORCE ROW LEVEL SECURITY` in a new migration
        // with the reason in its docblock — and expect
        // WorkspaceRlsCoverageTest::test_every_rls_enabled_table_is_forced to
        // demand an exemption entry.
    }

    // Verification (run after migrate):
    //   -- must return zero rows:
    //   SELECT n.nspname || '.' || c.relname
    //     FROM pg_class c
    //     JOIN pg_namespace n ON n.oid = c.relnamespace
    //    WHERE c.relkind IN ('r', 'p')
    //      AND c.relrowsecurity
    //      AND NOT c.relforcerowsecurity
    //      AND n.nspname NOT IN ('pg_catalog', 'information_schema');
    //   -- spot-check a previously ENABLE-only table:
    //   SELECT relrowsecurity, relforcerowsecurity
    //     FROM pg_class WHERE oid = 'silver.workspaces'::regclass;
};
