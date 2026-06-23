<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Move public.query_audit_log → audit.query_audit_log
 * ================================================================
 *
 * Closes a §05 step 6 misalignment that has been live since the table was
 * first created in 2026_04_12. §05 step 6 mandates:
 *
 *   "Final response + citation graph + retrieval trace stored to a separate
 *    app/audit Postgres schema (not the geological domain schema set in
 *    §04e) for audit / replay. Operational product data and geology data
 *    must not be muddled."
 *
 * The original migration placed query_audit_log in `public`, which:
 *   1. Mixes audit data with Laravel framework tables (failed_jobs, jobs,
 *      cache, sessions, personal_access_tokens, …).
 *   2. Means `georag_audit`'s INSERT-only grant has to be table-specific
 *      rather than schema-wide — every new audit table needs its own grant.
 *   3. Bypasses the schema-level boundary that makes role separation
 *      enforceable at audit time ("show me everything georag_audit can
 *      touch" = list every table it has explicit grants on, vs. "list one
 *      schema").
 *
 * What this migration does
 * ------------------------
 *   1. CREATE SCHEMA IF NOT EXISTS audit (idempotent — init-postgis.sql also
 *      creates it on fresh-init, but this guard handles existing dev
 *      clusters where the init script ran before the audit schema was added).
 *   2. ALTER TABLE ... SET SCHEMA — atomic metadata-only operation; indexes,
 *      constraints, sequences, RLS policies all follow the table to the new
 *      schema.
 *   3. Re-issue the table-level grants at the new location for the three
 *      service roles (georag_read, georag_write, georag_audit).
 *   4. Set ALTER DEFAULT PRIVILEGES so future audit tables created by the
 *      `georag` migration user auto-inherit the correct grants — Laravel
 *      Eloquent migrations can just `Schema::create('audit.next_table')`
 *      without remembering to GRANT.
 *
 * Operational impact
 * ------------------
 *   * Atomic — `ALTER TABLE ... SET SCHEMA` is a single catalogue update.
 *     No table rewrite, no data movement.
 *   * Application code references `query_audit_log` either via the Eloquent
 *     model (updated in this PR to `audit.query_audit_log`) or via Postgres
 *     search_path (updated in init-postgis.sql to include `audit`).
 *   * Existing search_path on running containers persists in postgresql.auto.conf
 *     until next restart; the migration is still safe because the Eloquent
 *     model uses the schema-qualified name.
 *
 * Rollback (down)
 * ---------------
 *   ALTER TABLE ... SET SCHEMA public is symmetric and equally fast. The
 *   audit schema is intentionally NOT dropped — other audit tables may
 *   accumulate there in future migrations and they should outlive any
 *   single rollback.
 *
 * References
 * ----------
 *   georag-architecture.html §05 step 6 (audit-schema separation rule)
 *   docker/postgresql/init/init-postgis.sql (audit schema creation)
 *   docker/postgresql/init-roles.sql (georag_audit role + ALTER DEFAULT PRIVILEGES)
 *   ops/runbooks/RUNBOOK.md (audit operations: dump, encrypt, restore, rotate-key)
 */
return new class extends Migration
{
    public function up(): void
    {
        // SQLite (used by the PHPUnit suite) has no schema concept and no
        // ALTER TABLE ... SET SCHEMA. Skip the schema move entirely under
        // SQLite — the test DB keeps the table under its original bare name
        // `query_audit_log` (the create migration and every ALTER in the
        // chain use that name on all drivers). Migrations that touch this
        // table after the move must therefore branch on the driver and use
        // the bare `query_audit_log` name under SQLite.
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // 1. Schema. Idempotent — init-postgis.sql creates it too, but
        //    repeating here lets this migration succeed on a cluster whose
        //    init scripts predate that change.
        DB::statement('CREATE SCHEMA IF NOT EXISTS audit');

        // 2. USAGE for the three service roles. Re-grants are no-ops if
        //    init-roles.sql already issued them; this guards against a
        //    cluster where init-roles.sql is older than this migration.
        DB::statement('GRANT USAGE ON SCHEMA audit TO georag_read, georag_write, georag_audit');

        // 3. Move the table. IF EXISTS handles two cases:
        //    - Re-runs after the table is already in audit (no-op).
        //    - Fresh installs where create_query_audit_log_table.php ran
        //      before this one (table is in public, gets moved here).
        //    Indexes, constraints, sequences, and any RLS policies follow.
        DB::statement('ALTER TABLE IF EXISTS public.query_audit_log SET SCHEMA audit');

        // 4. Re-issue the table-level grants at the new location. (init-roles
        //    granted INSERT on public.query_audit_log; that grant doesn't
        //    follow SET SCHEMA in older PG versions and is harmless to
        //    re-issue on the new path either way.)
        DB::statement('GRANT INSERT ON audit.query_audit_log TO georag_audit, georag_write');
        DB::statement('GRANT UPDATE ON audit.query_audit_log TO georag_write');
        DB::statement('GRANT SELECT ON audit.query_audit_log TO georag_read, georag_write');

        // 5. Default privileges for FUTURE tables under audit. The migration
        //    runs as the `georag` user, so future tables created by the same
        //    user inherit these. This makes the next audit-table migration
        //    a one-liner: Schema::create('audit.foo', ...) and the grants
        //    are automatic. Idempotent — re-running has no effect.
        DB::statement('ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT INSERT ON TABLES TO georag_audit');
        DB::statement('ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT INSERT, UPDATE, SELECT ON TABLES TO georag_write');
        DB::statement('ALTER DEFAULT PRIVILEGES IN SCHEMA audit GRANT SELECT ON TABLES TO georag_read');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Symmetric move back. Schema is intentionally not dropped — other
        // audit tables may have accumulated. Re-issue the historical grant.
        DB::statement('ALTER TABLE IF EXISTS audit.query_audit_log SET SCHEMA public');
        DB::statement('GRANT INSERT ON public.query_audit_log TO georag_audit');
    }
};
