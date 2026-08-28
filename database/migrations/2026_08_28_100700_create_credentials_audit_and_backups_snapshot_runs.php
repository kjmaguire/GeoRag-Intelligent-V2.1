<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Port the last two straightforwardly portable raw-only tables.
 *
 *   audit.integration_credentials_audit  Layer H — OAuth/token lifecycle audit
 *   backups.snapshot_runs                §11.1 — per-store backup run registry
 *
 * From `database/raw/phase0/80-layer-h-credentials-audit.sql` and
 * `database/raw/phase0/103-section11-backups-schema.sql`, neither in
 * `manifest.json`, so neither has ever run on Azure.
 *
 * ## The credentials-audit table is not dormant — it is blocking a manifest file
 *
 * Nothing in `src/` or `app/` reads or writes this table; the Credential
 * Health Agent that will is Phase 2. On its own that would make it the least
 * urgent entry left. It is not, because of what its ABSENCE does to a file
 * that IS in the manifest.
 *
 * `raw/phase0/98-rls-tenant-isolation-block3.sql` is manifest entry 4, so
 * `php artisan db:apply-raw` runs it. At its line 123 it does
 *
 *     ALTER TABLE audit.integration_credentials_audit ENABLE ROW LEVEL SECURITY;
 *
 * and the table is not there. The file is wrapped `BEGIN;` (line 36) …
 * `COMMIT;` (line 419), and `ApplyRawSql` executes it whole via
 * `unprepared()` — "one transaction per file, failing loudly".
 *
 * The consequence is not that the statements after line 123 are skipped. It
 * is that NONE of the file applies. Reproduced on PostgreSQL 16: the `42P01`
 * aborts the transaction, every later statement returns `25P02
 * in_failed_sql_transaction`, and the closing `COMMIT` is executed by the
 * server as a ROLLBACK — so the statements BEFORE line 123, which had
 * succeeded, are undone as well. All 65 of block 3's `ALTER` / `CREATE
 * POLICY` / `CREATE INDEX` / `UPDATE` statements come back off: the audit,
 * ops, workflow and targeting RLS enables and policies, the per-partition
 * FORCE that RLS does not inherit, and the five `workspace_id` B-tree indexes
 * the file exists to add. `ApplyRawSql` does report it — it prints
 * `FAILED <file>` and returns a non-zero exit — so this is a loud failure
 * that leaves the cluster exactly as it was, not a silent partial apply.
 *
 * `manifest.json` has a guard for exactly this. Its `_doc` says a file earns
 * its place "by declaring the relations it operates on so a database that
 * lacks them is reported rather than half-applied", and `ApplyRawSql::
 * missingPrerequisites()` skips a file whose `requires` are absent. Entry 4
 * declares `audit.audit_ledger`, `workflow.workflow_runs`,
 * `targeting.target_scores` and `ops.support_tickets` — but not this table,
 * the one relation in the file that no other path creates. The guard is
 * correct and simply was not told about this table, so it never fired.
 *
 * This migration fixes it at the root: once the table exists in the chain,
 * block 3 applies cleanly. The manifest entry gains the missing `requires`
 * name in the same commit so the guard is honest either way.
 *
 * ## RLS on the credentials audit
 *
 * The raw file that CREATEs it (80) adds no RLS at all, and
 * `phase0/95-rls-policies.sql` lists it under "Tables that DO NOT get RLS
 * ... (admin-only — gated by RBAC, not RLS)". Both are stale: block 3 later
 * enabled and forced RLS on it with a named policy. Following 80 alone would
 * create a table with a `workspace_id` column and no policy, which
 * `WorkspaceRlsCoverageTest::test_every_workspace_scoped_table_has_rls_with_a_policy`
 * fails — the `audit` schema is in its sweep and this table is not on its
 * exempt list.
 *
 * So the policy is ported from block 3, byte-for-byte including its lack of a
 * `NULLIF` wrapper. That shape has a real edge — `current_setting('app.
 * workspace_id', true)::uuid` raises `22P02 invalid_text_representation` on
 * the empty-string sentinel that `BindWorkspaceRlsContext` binds when it
 * cannot resolve a workspace, rather than returning no rows. It is ported
 * anyway and deliberately: block 3 DROPs and re-CREATEs this policy by name
 * every time it runs, so a different shape here would flip back and forth
 * depending on whether an operator had run `db:apply-raw`. Fixing the shape
 * means changing block 3 and this migration together, which is a separate
 * change; the table has no readers today, so the edge is latent.
 *
 * ## backups.snapshot_runs
 *
 * Platform-level, no `workspace_id`, no RLS — and `WorkspaceRlsCoverageTest`
 * excludes the whole `backups` schema from its sweep, so that is consistent
 * rather than a gap. `routers/admin_tier234.py::list_snapshot_runs` probes
 * `information_schema` before querying, so its absence degrades to an empty
 * listing rather than a 500; this makes the listing real.
 *
 * The `store` CHECK keeps `'neo4j'` even though `backup_neo4j` was deleted on
 * 2026-08-19 and nothing writes that value any more. Dropping it would be a
 * behaviour change rather than a cleanup: dev clusters that ran the raw file
 * can hold historical rows with `store = 'neo4j'`, and a narrowed CHECK is
 * validated against existing data, so the migration would fail on exactly
 * those clusters. Same reasoning that kept the retired tools in the §4
 * registry seed.
 *
 * ## Split ownership
 *
 * `backups` is one of only three schemas `database/raw/phase0/` creates for
 * itself, and measured against the `Migrations under production privileges`
 * job it is one of the two that `bootstrap` ends up owning. So the same
 * stand-down 2026_08_28_100400 needed applies here: `CREATE TABLE IF NOT
 * EXISTS` in a schema this role cannot CREATE in raises `permission denied
 * for schema`, because Postgres resolves the creation namespace before the
 * IF NOT EXISTS test. `audit` is created by migrations
 * (2026_05_14_140000), so it needs no such guard.
 *
 * Idempotent: `IF NOT EXISTS` throughout, `DROP POLICY IF EXISTS` first.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $this->createCredentialsAudit();
        $this->createSnapshotRuns();
    }

    private function createCredentialsAudit(): void
    {
        // integration_id is free-form text, not a FK: the integrations table
        // itself is Phase 2 and Phase 0 declined the forward dependency.
        // credential_ref is an opaque pointer — never secret material.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS audit.integration_credentials_audit (
                id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id     uuid        NULL,
                integration_id   text        NULL,
                integration_kind text        NOT NULL,
                action           text        NOT NULL,
                credential_ref   text        NULL,
                expires_at       timestamptz NULL,
                actor_id         bigint      NULL,
                actor_kind       text        NOT NULL DEFAULT 'system',
                payload          jsonb       NOT NULL DEFAULT '{}'::jsonb,
                occurred_at      timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT integration_credentials_audit_action_check CHECK (
                    action IN (
                        'created','refreshed','expired','rotated',
                        'revoked','failed_refresh','manual_reset'
                    )
                ),
                CONSTRAINT integration_credentials_audit_actor_kind_check CHECK (
                    actor_kind IN ('user','system','agent','integration')
                )
            )
        SQL);

        foreach ([
            "COMMENT ON TABLE audit.integration_credentials_audit IS
                'OAuth/token lifecycle audit trail. Phase 0 deploys table; Phase 2 deploys agents that write to it.'",
            "COMMENT ON COLUMN audit.integration_credentials_audit.credential_ref IS
                'Opaque pointer (e.g. integrations.id, KMS key alias). The secret material itself is never stored here.'",
            "COMMENT ON COLUMN audit.integration_credentials_audit.payload IS
                'Non-secret context only: granted scopes, refresh interval, error categorisation, etc.'",
        ] as $comment) {
            DB::statement($comment);
        }

        foreach ([
            'CREATE INDEX IF NOT EXISTS integration_credentials_audit_workspace_idx
                ON audit.integration_credentials_audit (workspace_id, occurred_at DESC)',
            'CREATE INDEX IF NOT EXISTS integration_credentials_audit_integration_idx
                ON audit.integration_credentials_audit (integration_id, occurred_at DESC)
             WHERE integration_id IS NOT NULL',
            'CREATE INDEX IF NOT EXISTS integration_credentials_audit_action_idx
                ON audit.integration_credentials_audit (action, occurred_at DESC)',
            'CREATE INDEX IF NOT EXISTS integration_credentials_audit_kind_idx
                ON audit.integration_credentials_audit (integration_kind, occurred_at DESC)',
            // Block 3 adds this one alongside the policy; ported so the two
            // agree and so the §11.5 index gate sees a workspace_id index.
            'CREATE INDEX IF NOT EXISTS idx_integration_credentials_audit_workspace_id
                ON audit.integration_credentials_audit (workspace_id)',
        ] as $index) {
            DB::statement($index);
        }

        // Ported byte-for-byte from block 3 — see the class docblock on why
        // the missing NULLIF is preserved rather than corrected here.
        DB::statement('ALTER TABLE audit.integration_credentials_audit ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE audit.integration_credentials_audit FORCE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            DROP POLICY IF EXISTS integration_credentials_audit_workspace_isolation
                ON audit.integration_credentials_audit
        SQL);
        DB::statement(<<<'SQL'
            CREATE POLICY integration_credentials_audit_workspace_isolation
                ON audit.integration_credentials_audit
                USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
                WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid)
        SQL);
    }

    private function createSnapshotRuns(): void
    {
        DB::statement('CREATE SCHEMA IF NOT EXISTS backups');

        if ($this->backupsSchemaIsForeignOwned()) {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS backups.snapshot_runs (
                run_id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                store          text        NOT NULL,
                started_at     timestamptz NOT NULL DEFAULT now(),
                completed_at   timestamptz,
                bucket         text,
                object_key     text,
                sha256_hex     text,
                bytes          bigint,
                status         text        NOT NULL DEFAULT 'running',
                failure_reason text,
                payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
                created_at     timestamptz NOT NULL DEFAULT now(),
                updated_at     timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT snapshot_runs_store_check CHECK (
                    store IN ('postgres', 'neo4j', 'qdrant', 'redis', 'seaweedfs')
                ),
                CONSTRAINT snapshot_runs_status_check CHECK (
                    status IN ('running', 'completed', 'failed')
                )
            )
        SQL);
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_snapshot_runs_store_started
                ON backups.snapshot_runs (store, started_at DESC)
        SQL);
        // In-flight runs across all stores, for the operator dashboard.
        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_snapshot_runs_running
                ON backups.snapshot_runs (started_at DESC)
             WHERE status = 'running'
        SQL);
        DB::statement(<<<'SQL'
            COMMENT ON TABLE backups.snapshot_runs IS
                '§11.1 — per-store backup snapshot run registry. One row per cron invocation. cross-tenant; admin-gated.'
        SQL);

        // Admin endpoints read via georag_app; the backup cron writes on the
        // direct georag connection, bypassing PgBouncer.
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
                    GRANT USAGE ON SCHEMA backups TO georag_app;
                    GRANT SELECT, INSERT, UPDATE ON backups.snapshot_runs TO georag_app;
                END IF;
            END $$;
        SQL);
    }

    /**
     * True when `backups` exists under an owner this role is not a member of
     * and the table is already there — the db:apply-raw-then-migrate cluster
     * the class docblock describes. Warns and lets the caller return.
     *
     * Looked up through pg_namespace/pg_class by NAME rather than
     * to_regclass(), which itself needs USAGE on the schema.
     */
    private function backupsSchemaIsForeignOwned(): bool
    {
        $schema = DB::selectOne(
            "SELECT pg_has_role(current_user, n.nspowner, 'USAGE') AS mine,
                    pg_get_userbyid(n.nspowner)                    AS owner
               FROM pg_namespace n WHERE n.nspname = 'backups'",
        );

        if ($schema === null || $schema->mine) {
            return false;
        }

        $canCreate = DB::selectOne(
            "SELECT has_schema_privilege(current_user, 'backups', 'CREATE') AS ok",
        );

        if ($canCreate !== null && $canCreate->ok) {
            return false;
        }

        DB::statement(<<<'SQL'
            DO $$
            DECLARE
                owner_name text;
            BEGIN
                SELECT pg_get_userbyid(nspowner) INTO owner_name
                  FROM pg_namespace WHERE nspname = 'backups';

                RAISE WARNING 'schema backups is owned by another role (%) and this role '
                    'cannot CREATE in it — skipping backups.snapshot_runs. This cluster '
                    'applied database/raw/phase0/103 before migrating, so the table is '
                    'already present in the raw file''s shape. Re-run as the owning role, '
                    'or reassign ownership, if that changes.', owner_name;
            END $$;
        SQL);

        return true;
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $mine = DB::selectOne(
            "SELECT pg_has_role(current_user, c.relowner, 'USAGE') AS mine
               FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = 'backups' AND c.relname = 'snapshot_runs'",
        );

        if ($mine === null || $mine->mine) {
            DB::statement('DROP TABLE IF EXISTS backups.snapshot_runs');
        }

        DB::statement('DROP TABLE IF EXISTS audit.integration_credentials_audit');
    }
};
