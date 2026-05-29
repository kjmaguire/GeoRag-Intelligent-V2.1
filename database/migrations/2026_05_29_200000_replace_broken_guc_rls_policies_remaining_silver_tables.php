<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * SECURITY FIX 2026-05-28 — close the legacy-GUC tail on five more
 * silver RLS policies missed by the 2026-05-25 broken-GUC sweep
 * (2026_05_25_180924 + 2026_05_25_182857).
 *
 * **The bug.** Five silver tables carry a `<table>_workspace_isolation`
 * policy whose USING clause references the legacy
 * `current_setting('georag.workspace_id', true)` GUC. Every active
 * codepath (Laravel SetsWorkspaceRlsContext, FastAPI deps + agents +
 * audit, Dagster ShadowRouter) sets `app.workspace_id` instead, so
 * these policies are functionally fail-open — the workspace_id-vs-GUC
 * comparison resolves to text-vs-empty-string which always returns
 * false, but the policy is otherwise the only filter on the row, so
 * everything is visible to every tenant. Discovered by
 * `WorkspaceRlsCoverageTest::test_no_policy_references_legacy_georag_gucs`
 * after the original sweep landed.
 *
 *   silver.alias_gaps           — alias-gap detector queue
 *   silver.data_quality_flags   — DQ rule violations
 *   silver.document_versions    — NI 43-101 version chain
 *   silver.entity_aliases       — alias dictionary
 *   silver.query_traces         — agentic retrieval trace log
 *
 * **Why these slipped through 2026_05_25_180924.** That migration
 * targeted tables whose ONLY policy used the broken `georag.*` GUC
 * (those policies were named `*_tenant_scope` / `*_project_scope`).
 * These five already carried a `*_workspace_isolation` policy with
 * the canonical NAME but the wrong GUC inside — so the catalog-level
 * "table has at least one policy" check was happy, but the policy
 * itself was still fail-open.
 *
 * **The fix.** Drop and re-create each policy with the canonical
 * shape that reads `app.workspace_id` and falls open only when the
 * GUC is unset (empty), mirroring 2026_05_25_180924 and
 * 2026_05_29_190000.
 *
 * SQLite (test DB) does not support RLS — gated on Postgres.
 */
return new class extends Migration
{
    /**
     * Tables whose `<table>_workspace_isolation` policy must be
     * replaced. Each has `workspace_id` directly on the row, so the
     * canonical shape is the simple self-check; no EXISTS joins.
     *
     * @var list<string>
     */
    private const TABLES = [
        'alias_gaps',
        'data_quality_flags',
        'document_versions',
        'entity_aliases',
        'query_traces',
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::TABLES as $tbl) {
            if (! $this->tableExists($tbl)) {
                continue;
            }
            $policy = $this->canonicalPolicyName($tbl);

            DB::statement("ALTER TABLE silver.{$tbl} ENABLE ROW LEVEL SECURITY");
            DB::statement("DROP POLICY IF EXISTS {$policy} ON silver.{$tbl}");
            DB::statement(<<<SQL
                CREATE POLICY {$policy} ON silver.{$tbl}
                  USING (
                    NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                    OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                  )
            SQL);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Best-effort reversal — drops the canonical policies this
        // migration installed. We do NOT re-create the broken-GUC
        // versions; rolling back leaves the tables RLS-on with no
        // policy, which under PG semantics means owners see all rows
        // and non-owners see none. Safer than restoring a fail-open
        // bug.
        foreach (self::TABLES as $tbl) {
            $policy = $this->canonicalPolicyName($tbl);
            DB::statement("DROP POLICY IF EXISTS {$policy} ON silver.{$tbl}");
        }
    }

    private function canonicalPolicyName(string $table): string
    {
        return "{$table}_workspace_isolation";
    }

    private function tableExists(string $table): bool
    {
        return DB::table('information_schema.tables')
            ->where('table_schema', 'silver')
            ->where('table_name', $table)
            ->exists();
    }
};
