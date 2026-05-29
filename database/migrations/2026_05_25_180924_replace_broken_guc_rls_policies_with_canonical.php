<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * SECURITY FIX 2026-05-25 — close cross-tenant leak on 15 silver tables
 * whose ONLY RLS policy checked a GUC the app has never set, leaving
 * them functionally fail-open.
 *
 * **The bug.** Twelve silver tables carry `*_tenant_scope` or
 * `*_project_scope` policies that check
 * ``current_setting('georag.workspace_id', true)`` or
 * ``current_setting('georag.project_id', true)`` — but every active
 * codepath (Laravel SetsWorkspaceRlsContext, FastAPI deps + agents +
 * audit, Dagster ShadowRouter) sets ``app.workspace_id``. The legacy
 * ``georag.*`` GUC names exist in exactly one stale path
 * (src/fastapi/app/agent/deps.py) that no production query exercises.
 *
 * Result: the broken policies' `NULLIF(current_setting(...) IS NULL`
 * branch is always true → the OR short-circuits → every row visible
 * to every tenant. Among the affected tables:
 *
 *   silver.document_passages      — RAG chunks (cross-corpus leak)
 *   silver.answer_runs            — chat history
 *   silver.answer_citation_items  — citation provenance
 *   silver.answer_citation_spans  — citation offsets
 *   silver.answer_retrieval_items — retrieval items
 *   silver.assay_results          — assay numerics
 *   silver.assay_events           — assay submissions
 *   silver.evidence_items         — evidence DAGs
 *   silver.geochemistry           — geochem analysis
 *   silver.message_feedback       — chat feedback
 *   silver.document_revisions     — revisions
 *   silver.samples                — samples (both *_tenant + *_project broken)
 *
 * **The fix.** Replace each broken policy with the canonical
 * `<table>_workspace_isolation` shape that uses `app.workspace_id`,
 * mirroring 2026_05_19_180100_enable_rls_on_uncovered_workspace_tables.
 * Three tables already have a working canonical policy
 * (drill_traces, mineral_claims, review_audit_log) — we just drop
 * the broken sibling so the catalog stays clean.
 *
 * **Why now.** Discovered during the deferred-items pass after the
 * bronze tenancy work. The Lakehouse audit only caught tables with
 * NO RLS at all; tables with broken-GUC policies registered as
 * "RLS on, policy present" in pg_policies and slipped through the
 * coverage test. WorkspaceRlsCoverageTest is being strengthened in
 * a sibling change to catch the wrong-GUC pattern too.
 *
 * SQLite (test DB) does not support RLS — gated on Postgres.
 *
 * **Forward compat.** All 15 tables have a `workspace_id UUID NOT NULL`
 * column (verified 2026-05-25). The policy is workspace-scoped only;
 * tables that previously had project-level scoping (mineral_claims,
 * samples) are intentionally relaxed to workspace scope because
 * (a) the project-scope policies were never actually enforced and
 * (b) workspace_id is the canonical tenancy boundary throughout the
 * stack. App-layer code is free to add explicit project_id filters
 * for finer scoping; the RLS layer is the tenancy floor.
 */
return new class extends Migration
{
    /**
     * Tables that need a canonical workspace_isolation policy installed
     * (and broken policies dropped).
     *
     * NOTE — silver.collars, silver.drill_traces, silver.mineral_claims,
     * silver.review_audit_log were initially omitted from this list.
     * The first three because the original audit query only saw their
     * production state where the broken policies were already dropped
     * by phase0 raw SQL (96-rls-tenant-isolation-block1.sql); the test
     * DB still has them. They're handled in the sibling migration
     * 2026_05_25_181500_replace_broken_guc_rls_policies_missed_tables.
     *
     * @var list<string>
     */
    private const NEEDS_CANONICAL = [
        'answer_citation_items',
        'answer_citation_spans',
        'answer_retrieval_items',
        'answer_runs',
        'assay_events',
        'assay_results',
        'document_passages',
        'document_revisions',
        'evidence_items',
        'geochemistry',
        'message_feedback',
        'samples',
    ];

    /**
     * Tables that already have a working canonical policy in production
     * (via phase0 raw SQL) — we only drop the broken sibling there for
     * catalog hygiene. Test-DB installs handled by the sibling migration.
     *
     * @var list<array{table: string, drop: list<string>}>
     */
    private const DROP_ONLY = [
        ['table' => 'drill_traces',     'drop' => ['drill_traces_tenant_scope']],
        ['table' => 'mineral_claims',   'drop' => ['mineral_claims_project_scope']],
        ['table' => 'review_audit_log', 'drop' => ['review_audit_log_project_scope']],
    ];

    /**
     * Mapping of table → broken policies to drop before installing the
     * canonical one. Built once so up() + down() stay in sync.
     *
     * @var array<string, list<string>>
     */
    private const BROKEN_POLICIES = [
        'answer_citation_items' => ['answer_citation_items_tenant_scope'],
        'answer_citation_spans' => ['answer_citation_spans_tenant_scope'],
        'answer_retrieval_items' => ['answer_retrieval_items_tenant_scope'],
        'answer_runs' => ['answer_runs_tenant_scope'],
        'assay_events' => ['assay_events_tenant_scope'],
        'assay_results' => ['assay_results_tenant_scope'],
        'document_passages' => ['document_passages_tenant_scope'],
        'document_revisions' => ['document_revisions_tenant_scope'],
        'evidence_items' => ['evidence_items_tenant_scope'],
        'geochemistry' => ['geochemistry_tenant_scope'],
        'message_feedback' => ['message_feedback_tenant_scope'],
        'samples' => ['samples_tenant_scope', 'samples_project_scope'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Pass 1 — replace broken policies with the canonical shape.
        // Skip tables missing from this environment (test-DB lacks
        // phase0 raw-SQL tables like silver.assay_events). The
        // WorkspaceRlsCoverageTest's no-legacy-GUC assertion catches
        // any production regression regardless of the migration path.
        foreach (self::NEEDS_CANONICAL as $tbl) {
            if (! $this->tableExists($tbl)) {
                continue;
            }
            // Drop broken policies regardless — they're invalid catalog
            // hygiene in any environment that has the policy installed.
            foreach (self::BROKEN_POLICIES[$tbl] ?? [] as $broken) {
                DB::statement("DROP POLICY IF EXISTS {$broken} ON silver.{$tbl}");
            }
            // Only install the canonical policy when workspace_id is
            // present on the table. The test DB lacks workspace_id on
            // several silver tables (added by phase0 raw SQL in
            // production); CREATE POLICY would fail there.
            if ($this->hasWorkspaceIdColumn($tbl)) {
                $this->installCanonicalPolicy($tbl);
            }
        }

        // Pass 2 — drop redundant broken policies on tables where the
        // canonical already exists. No-op installs; no behavior change.
        foreach (self::DROP_ONLY as $entry) {
            if (! $this->tableExists($entry['table'])) {
                continue;
            }
            foreach ($entry['drop'] as $broken) {
                DB::statement("DROP POLICY IF EXISTS {$broken} ON silver.{$entry['table']}");
            }
        }
    }

    private function tableExists(string $table): bool
    {
        return DB::table('information_schema.tables')
            ->where('table_schema', 'silver')
            ->where('table_name', $table)
            ->exists();
    }

    private function hasWorkspaceIdColumn(string $table): bool
    {
        return DB::table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', $table)
            ->where('column_name', 'workspace_id')
            ->exists();
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Best-effort reversal — drops the canonical policies this
        // migration installed. We do NOT re-create the broken-GUC
        // versions; rolling back this migration leaves the tables
        // covered by ENABLE ROW LEVEL SECURITY with no policy, which
        // (under PG semantics) means owners see all rows and
        // non-owners see none. That's a safer default than restoring
        // a fail-open policy. Re-install via the next migration if
        // needed.
        foreach (self::NEEDS_CANONICAL as $tbl) {
            $policy = $this->canonicalPolicyName($tbl);
            DB::statement("DROP POLICY IF EXISTS {$policy} ON silver.{$tbl}");
        }
    }

    private function canonicalPolicyName(string $table): string
    {
        return "{$table}_workspace_isolation";
    }

    private function installCanonicalPolicy(string $table): void
    {
        // RLS is presumably already enabled (these tables have broken
        // policies — they must already be RLS-on). ALTER is idempotent
        // either way.
        DB::statement("ALTER TABLE silver.{$table} ENABLE ROW LEVEL SECURITY");

        $policy = $this->canonicalPolicyName($table);

        // Re-creating an existing policy errors; drop-then-create
        // keeps the migration idempotent across re-runs.
        DB::statement("DROP POLICY IF EXISTS {$policy} ON silver.{$table}");
        DB::statement(<<<SQL
            CREATE POLICY {$policy} ON silver.{$table}
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }
};
