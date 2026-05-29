<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Module 9 Chunk 9.3 — extend GUC-aware RLS coverage from 2 tables (collars +
 * samples) to all 11 workspace-scoped silver tables.
 *
 * Closes audit finding A3-01 (HIGH) from
 * `ops/audit/2026-04-22-security-rbac-audit.md`. Pairs with the deps.py
 * change that adds `SET LOCAL georag.workspace_id` alongside the existing
 * `georag.project_id` GUC.
 *
 * Policy pattern (mirrors `2026_04_17_120200_replace_toothless_rls...`):
 *
 *   USING (
 *     current_setting('georag.<key>', true) IS NULL
 *     OR <key> = current_setting('georag.<key>', true)::uuid
 *   )
 *
 * The `IS NULL` branch keeps the single-tenant escape hatch in place — Dagster
 * and admin scripts run without the GUC and see all rows.  In multi-tenant
 * mode FastAPI sets the GUC at the top of every transaction so cross-tenant
 * SELECTs return zero rows even if a tool forgets a WHERE clause.
 *
 * Per-table scoping decisions (verified against live schema 2026-04-22):
 *   silver.drill_traces       — project_id GUC  (parallel spatial w/ collars)
 *   silver.evidence_items     — workspace_id GUC
 *   silver.answer_runs        — workspace_id GUC  (audit; project_id nullable)
 *   silver.answer_retrieval_items  — workspace_id GUC
 *   silver.answer_citation_items   — workspace_id GUC
 *   silver.answer_citation_spans   — workspace_id GUC
 *   silver.document_revisions      — workspace_id GUC
 *   silver.document_passages       — workspace_id GUC
 *   silver.message_feedback        — workspace_id GUC
 *
 * pgTAP regression: database/tests/pgtap/11_rls_workspace_isolation.sql.
 */
return new class extends Migration
{
    /**
     * @var array<string,string>  table => GUC key
     */
    private array $project_scoped = [
        'drill_traces' => 'project_id',
    ];

    /**
     * @var array<string,string>  table => GUC key
     */
    private array $workspace_scoped = [
        'evidence_items'         => 'workspace_id',
        'answer_runs'            => 'workspace_id',
        'answer_retrieval_items' => 'workspace_id',
        'answer_citation_items'  => 'workspace_id',
        'answer_citation_spans'  => 'workspace_id',
        'document_revisions'     => 'workspace_id',
        'document_passages'      => 'workspace_id',
        'message_feedback'       => 'workspace_id',
    ];

    public function up(): void
    {
        foreach ($this->project_scoped as $table => $col) {
            $this->applyPolicy($table, $col, 'project_id');
        }
        foreach ($this->workspace_scoped as $table => $col) {
            $this->applyPolicy($table, $col, 'workspace_id');
        }

        // pgTAP regression tests SET ROLE martin_readonly to exercise the
        // RLS policy under a non-superuser session (PG superusers bypass
        // RLS even with FORCE ROW LEVEL SECURITY). Grant SELECT on every
        // policy-bearing table so the tests can read.
        // The application-side connection runs as the georag service role
        // which is also non-superuser in production; ensuring martin_readonly
        // mirrors that surface keeps the test parity.
        foreach (array_merge($this->project_scoped, $this->workspace_scoped) as $table => $_col) {
            DB::statement("GRANT SELECT ON silver.{$table} TO martin_readonly");
        }
    }

    public function down(): void
    {
        foreach (array_merge($this->project_scoped, $this->workspace_scoped) as $table => $_col) {
            DB::statement("DROP POLICY IF EXISTS {$table}_tenant_scope ON silver.{$table}");
            DB::statement("ALTER TABLE silver.{$table} DISABLE ROW LEVEL SECURITY");
        }
    }

    /**
     * Idempotent — drop+recreate so re-runs are safe.
     *
     * @param string $table  Bare table name in the silver schema.
     * @param string $col    Column on the table that holds the tenant key.
     * @param string $guc    GUC name suffix — 'project_id' or 'workspace_id'.
     */
    private function applyPolicy(string $table, string $col, string $guc): void
    {
        DB::statement("ALTER TABLE silver.{$table} ENABLE ROW LEVEL SECURITY");
        DB::statement("ALTER TABLE silver.{$table} FORCE ROW LEVEL SECURITY");
        DB::statement("DROP POLICY IF EXISTS {$table}_tenant_scope ON silver.{$table}");

        $qual = "current_setting('georag.{$guc}', true) IS NULL "
              . "OR {$col} = current_setting('georag.{$guc}', true)::uuid";

        DB::statement("
            CREATE POLICY {$table}_tenant_scope
            ON silver.{$table}
            FOR ALL
            USING ({$qual})
            WITH CHECK ({$qual})
        ");
    }
};
