<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB parity gap, same class of bug as
 * 2026_08_14_025900_provision_review_audit_log_workspace_id_for_pgsql_test_db
 * (which 2026_08_14_030000's RLS migration tripped over on the CI
 * drift-gate job).
 *
 * Seven "child" tables were created by their Laravel migration WITHOUT a
 * `workspace_id` column — RLS on them originally scoped through an EXISTS
 * subquery against the parent row (decision_records / hypotheses /
 * target_scores). In every real deployment, `database/raw/phase0/
 * 96-rls-tenant-isolation-block1.sql` and `97-rls-tenant-isolation-
 * block2.sql` ADD a direct `workspace_id` column to these same tables
 * (Tier C "empty table" / Tier D "backfill from parent" blocks) and
 * replace the EXISTS-based policy with a direct-column one — verified
 * against the live `georag-postgresql` container 2026-08-15: all seven
 * tables carry a NOT NULL `workspace_id uuid` column with its own FK to
 * `silver.workspaces`/`workspaces`, not an EXISTS join. That direct-column
 * shape is what 2026_08_15_020100_close_rls_admin_escape_hatch_second_pass
 * converts to fail-closed.
 *
 * CI's schema-and-qdrant-drift job runs `php artisan migrate` against a
 * fresh Postgres with no phase0 bootstrap step, so these columns never
 * appear there. Without this migration, the fail-closed policy migration
 * would fail with "column workspace_id does not exist" the same way
 * 9fac505 did for silver.review_audit_log.
 *
 * Nullable, no backfill, no FK — mirrors the review_audit_log migration's
 * reasoning: every migrate-only environment starts with these tables
 * empty (RefreshDatabase), and application code (Python
 * bind_workspace_scope() call sites, the Laravel RecordDecision facade)
 * always supplies workspace_id explicitly on INSERT, so there is nothing
 * to backfill. Idempotent — ADD COLUMN IF NOT EXISTS keeps production a
 * strict no-op since prod already has the column from phase0 raw SQL.
 */
return new class extends Migration
{
    /**
     * @var list<array{schema: string, table: string}>
     */
    private const TABLES = [
        ['schema' => 'silver', 'table' => 'hypothesis_evidence_links'],
        ['schema' => 'silver', 'table' => 'decision_evidence_links'],
        ['schema' => 'silver', 'table' => 'decision_options'],
        ['schema' => 'silver', 'table' => 'decision_outcomes'],
        ['schema' => 'silver', 'table' => 'decision_lessons_learned'],
        ['schema' => 'targeting', 'table' => 'target_score_factors'],
        ['schema' => 'targeting', 'table' => 'target_uncertainties'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::TABLES as $t) {
            $tableExists = DB::table('information_schema.tables')
                ->where('table_schema', $t['schema'])
                ->where('table_name', $t['table'])
                ->exists();
            if (! $tableExists) {
                continue;
            }

            $hasColumn = DB::table('information_schema.columns')
                ->where('table_schema', $t['schema'])
                ->where('table_name', $t['table'])
                ->where('column_name', 'workspace_id')
                ->exists();
            if ($hasColumn) {
                continue;
            }

            DB::statement("ALTER TABLE {$t['schema']}.{$t['table']} ADD COLUMN workspace_id uuid");
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // CASCADE: verified live (2026-08-15, `php artisan migrate:rollback`
        // against georag_test) that a plain DROP COLUMN fails with
        // "SQLSTATE[2BP01] Dependent objects still exist" whenever this
        // migration's down() runs AFTER
        // 2026_08_15_020100_close_rls_admin_escape_hatch_second_pass's
        // down() has already restored a policy that references
        // workspace_id (the normal `migrate:rollback --step=2` order).
        // CASCADE additionally drops that policy — safe here because a
        // rollback of BOTH migrations together means the policy is being
        // torn down anyway, and this column never carries production data
        // in a migrate-only environment (see class docblock).
        foreach (self::TABLES as $t) {
            DB::statement("ALTER TABLE {$t['schema']}.{$t['table']} DROP COLUMN IF EXISTS workspace_id CASCADE");
        }
    }
};
