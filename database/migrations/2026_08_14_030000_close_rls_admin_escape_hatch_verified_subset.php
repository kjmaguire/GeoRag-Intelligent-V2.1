<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * DB audit item + security audit item (2026-08-14 night pass) — close the
 * fail-open "admin escape hatch" shape that EVERY workspace-scoped RLS
 * policy in this app currently has:
 *
 *   USING (
 *     NULLIF(current_setting('app.workspace_id', true), '') IS NULL
 *     OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
 *   )
 *
 * When `app.workspace_id` is unset, this ADMITS ALL ROWS instead of none.
 * It was deliberately retrofitted (2026_05_13_160000_retrofit_rls_admin_
 * escape_hatch.php and the 2026-05-25 `database/raw/phase0/111-112`
 * bulk-sweep) as an operability fix so admin/maintenance paths that never
 * set the GUC wouldn't get zero rows — but it means RLS provides no real
 * backstop: any request path that FORGETS to set the GUC silently sees
 * every workspace's data instead of erroring.
 *
 * SCOPE OF THIS MIGRATION — read before extending the table list.
 * ------------------------------------------------------------------
 * The fail-open shape is installed on ~90+ tables. A full-codebase static
 * audit (no live DB access available) found that MANY of those tables are
 * read or written by Laravel controllers / FastAPI routers that do a
 * "look up an arbitrary row by ID, then authorize at the app layer"
 * pattern (`hasProjectAccess()` and siblings — see EvidenceController,
 * TrustController, PublicApiController, CitationController and ~8 more;
 * FastAPI mirrors this in routers/{evidence,answer_runs,exports,projects,
 * queries,completeness,coverage}.py, none of which call the canonical
 * `scoped_connection()`/`bind_workspace_scope()` helper). Those lookups
 * run WITHOUT the workspace GUC set (by design — they don't know the
 * workspace until after the lookup), so flipping their tables to
 * fail-closed would return zero rows for legitimate, everyday users, not
 * just attackers. That is a much bigger, cross-cutting change than one
 * migration should carry without integration-testing against a live DB.
 *
 * This migration converts ONLY the subset of tables verified via static
 * analysis to have NO live Laravel request-time reader and NO FastAPI
 * request-time reader that bypasses `scoped_connection()` — i.e. tables
 * touched exclusively by Hatchet workflows (which connect as the `georag`
 * Postgres role — LOGIN SUPERUSER, unconditionally bypasses RLS per
 * docs/architecture/manual/11-tenancy-and-rls.md §3 / finding R-P0-10 —
 * so these policies are functionally inert for that traffic either way)
 * or by nothing at all in the current codebase.
 *
 * The much larger remaining set (silver.projects, collars, reports,
 * samples, answer_runs, evidence_items, document_passages, exports,
 * audit.audit_ledger, source_trust_scores, targeting.*, and more) is
 * intentionally left fail-open here and documented as an open risk in
 * the accompanying report — converting those needs a live-DB-backed pass
 * that can verify each Laravel/FastAPI call site sets the GUC (or add
 * one) before flipping the policy, not a static-analysis-only migration.
 *
 * Regression coverage: tests/Feature/Tenancy/RlsFailClosedSubsetTest.php
 * asserts every policy in TABLES below has no escape-hatch clause, so a
 * future migration can't silently reintroduce fail-open on this subset.
 */
return new class extends Migration
{
    /**
     * (schema, table, policy_name) triples verified fail-open today and
     * safe to flip — no NOT NULL workspace_id column edge case, no
     * live app-layer reader found. See class docblock for the audit
     * method.
     *
     * @var list<array{schema: string, table: string, policy: string}>
     */
    private const TABLES = [
        ['schema' => 'workspace', 'table' => 'workspace_memberships', 'policy' => 'tenant_isolation'],
        ['schema' => 'workspace', 'table' => 'workspace_agent_config', 'policy' => 'tenant_isolation'],
        ['schema' => 'workspace', 'table' => 'dry_run_outputs', 'policy' => 'tenant_isolation'],
        ['schema' => 'outbox', 'table' => 'pending_propagations', 'policy' => 'tenant_isolation'],
        ['schema' => 'outbox', 'table' => 'propagation_attempts', 'policy' => 'tenant_isolation'],
        ['schema' => 'usage', 'table' => 'usage_events', 'policy' => 'usage_events_tenant_isolation'],
        ['schema' => 'usage', 'table' => 'workspace_cost_ceilings', 'policy' => 'workspace_cost_ceilings_tenant_isolation'],
        ['schema' => 'silver', 'table' => 'kg_formation_aliases', 'policy' => 'kg_formation_aliases_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'kg_mineral_aliases', 'policy' => 'kg_mineral_aliases_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'kg_report_aliases', 'policy' => 'kg_report_aliases_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'kg_sample_aliases', 'policy' => 'kg_sample_aliases_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'collaboration_audit_log', 'policy' => 'collaboration_audit_log_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'collaboration_comments', 'policy' => 'collaboration_comments_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'agent_conversation_messages', 'policy' => 'agent_conversation_messages_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'agent_conversations', 'policy' => 'agent_conversations_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'pdf_coordinates', 'policy' => 'pdf_coordinates_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'pdf_layout_regions', 'policy' => 'pdf_layout_regions_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'pdf_ocr_results', 'policy' => 'pdf_ocr_results_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'pdf_table_cells', 'policy' => 'pdf_table_cells_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'pdf_text_blocks', 'policy' => 'pdf_text_blocks_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'pdf_vl_summaries', 'policy' => 'pdf_vl_summaries_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'review_audit_log', 'policy' => 'review_audit_log_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'assay_events', 'policy' => 'assay_events_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'ingest_extractions', 'policy' => 'tenant_isolation'],
        ['schema' => 'silver', 'table' => 'ingest_layouts', 'policy' => 'tenant_isolation'],
        ['schema' => 'silver', 'table' => 'ingest_ocr_results', 'policy' => 'tenant_isolation'],
        ['schema' => 'silver', 'table' => 'collab_anchors', 'policy' => 'silver_collab_anchors_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'tier3_unlock_requests', 'policy' => 'silver_tier3_unlock_requests_workspace_isolation'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::TABLES as $t) {
            // A handful of these tables (workspace.workspace_memberships,
            // workspace.workspace_agent_config, workspace.dry_run_outputs,
            // outbox.pending_propagations, outbox.propagation_attempts)
            // are created only by database/raw/phase0 bootstrap SQL, not
            // by a Laravel migration — per project_test_db_parity_gap,
            // they may not exist in a migrate-only Postgres test DB. Skip
            // gracefully rather than error; production/staging (which run
            // the phase0 bootstrap) get the real fix.
            if (! $this->tableExists($t['schema'], $t['table'])) {
                continue;
            }

            $qualified = "{$t['schema']}.{$t['table']}";

            // FORCE ROW LEVEL SECURITY so the policy also binds the table
            // owner (georag, via ad-hoc `psql -U georag` sessions outside
            // Hatchet/pgsql_migrations) — matches the pattern already used
            // on the majority of these tables. Idempotent.
            DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
            DB::statement("ALTER TABLE {$qualified} FORCE ROW LEVEL SECURITY");

            DB::statement("DROP POLICY IF EXISTS {$t['policy']} ON {$qualified}");
            DB::statement(<<<SQL
                CREATE POLICY {$t['policy']} ON {$qualified}
                    USING (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                    WITH CHECK (
                        workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                SQL);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Restore the exact fail-open shape these tables had before this
        // migration (the post-111/112-sweep canonical fail-open form).
        foreach (self::TABLES as $t) {
            if (! $this->tableExists($t['schema'], $t['table'])) {
                continue;
            }

            $qualified = "{$t['schema']}.{$t['table']}";

            DB::statement("DROP POLICY IF EXISTS {$t['policy']} ON {$qualified}");
            DB::statement(<<<SQL
                CREATE POLICY {$t['policy']} ON {$qualified}
                    USING (
                        NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                    WITH CHECK (
                        NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    )
                SQL);
        }
    }

    private function tableExists(string $schema, string $table): bool
    {
        return DB::table('information_schema.tables')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->exists();
    }
};
