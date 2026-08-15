<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Second pass on the RLS admin-escape-hatch closure started by
 * 2026_08_14_030000_close_rls_admin_escape_hatch_verified_subset. That
 * migration converted 28 tables verified via STATIC analysis only (no
 * live DB access) and deliberately left ~62 tables fail-open, including
 * everything under decision-intelligence, hypothesis-generation,
 * targeting, and saved_map_views.
 *
 * This pass has live DB access (`georag-postgresql` container, PG18,
 * full phase0-bootstrapped schema) and used it to verify BOTH that each
 * table actually has a `workspace_id` column of the right shape AND
 * that no live Laravel/FastAPI request-time reader depends on the
 * unset-GUC escape — not just static analysis.
 *
 * TABLES CONVERTED THIS PASS (15) — evidence per group:
 * ------------------------------------------------------------------
 * silver.hypotheses, silver.hypothesis_evidence_links
 *   Writer: app/services/geological_reasoning/hypothesis_generator.py
 *   already calls bind_workspace_scope() before both INSERTs (verified
 *   by reading the file — see the "0. Set the GUC" comment). No live
 *   reader found in Laravel (grep for `Hypothesis::` finds only the
 *   relation definition in HypothesisEvidenceLink) or FastAPI (only
 *   Hatchet workflows read/write, which connect as the `georag`
 *   superuser and bypass RLS regardless of policy shape).
 *
 * silver.decision_records, decision_evidence_links, decision_options,
 * decision_outcomes, decision_lessons_learned
 *   Both the Python facade (app.services.decision_intelligence.
 *   record_decision) and its Laravel mirror (App\Services\
 *   DecisionIntelligence\RecordDecision) are DEAD CODE today — grepped
 *   for callers in both trees and found none beyond tests and the
 *   Python module's own docstring example ("Live behavior lands when
 *   the eight capture hooks wire up" — doc-phase 92/133, never
 *   finished). The two live readers (FastAPI's
 *   support_cockpit/root_cause_investigation.py and support_packet.py)
 *   both use the ADR-0014 `lookup_and_rescope()` helper, which resolves
 *   the ticket's workspace_id and rebinds `app.workspace_id` BEFORE
 *   querying decision_records — the canonical scoped pattern, not the
 *   escape hatch. Hatchet workflows (field_outcome_learning.py,
 *   what_changed_detector.py) bypass RLS as `georag` regardless.
 *
 *   Drive-by fix alongside this migration: App\Services\
 *   DecisionIntelligence\RecordDecision::record() omitted `workspace_id`
 *   on the decision_evidence_links/decision_options/decision_outcomes
 *   INSERTs — those three columns are NOT NULL with no default (verified
 *   live), so the very first live call with evidence/options/an outcome
 *   would have thrown, independent of RLS. Fixed to match the Python
 *   facade, which already included it on all three.
 *
 * silver.saved_map_views
 *   No live create/read/update/delete path anywhere: no Laravel
 *   controller queries the SavedMapView model (grep finds only the
 *   model file itself + relation definitions), no FastAPI router
 *   touches `saved_map_views`, no frontend API call beyond a TS type
 *   declaration. ProjectController::destroy() relies on
 *   `ON DELETE CASCADE` from `projects` (not a direct query), and per
 *   PostgreSQL docs ("Row Security and Referential Integrity"),
 *   referential-integrity actions — including cascading deletes — ALWAYS
 *   bypass row security, so this fail-closed flip cannot break project
 *   deletion.
 *
 * targeting.target_recommendations, target_candidate_zones,
 * target_scores, target_score_factors, target_uncertainties,
 * target_review_decisions, target_outcomes
 *   target_recommendations had exactly one live reader:
 *   PublicApiController::targets() — the "look up by ID (hasProjectAccess
 *   on project_id), then app-layer-authorize" pattern the first pass's
 *   docblock warned about, never binding `app.workspace_id`. Fixed
 *   alongside this migration: the controller now resolves the project's
 *   workspace_id and wraps the query in
 *   SetsWorkspaceRlsContext::withWorkspaceRls() (same pattern as the
 *   2026-08-14 citation-IDOR fix on CitationController). The other six
 *   targeting tables have no live Laravel or FastAPI reader at all
 *   (Hatchet-only: train_target_model.py, continuous_learning_loop.py,
 *   field_outcome_learning.py, services/targeting/score_factors.py,
 *   services/target_scoring_ml/{ab_comparison,shap_writer}.py — none
 *   imported by any router).
 *
 *   Live-DB-verified bug found and fixed while testing this migration
 *   against `georag_test`: 5 of these 7 tables (target_recommendations,
 *   target_outcomes, target_review_decisions, target_scores,
 *   target_candidate_zones) carry a SECOND, differently-named permissive
 *   policy from 2026_06_03_010000_close_targeting_workflow_rls_gaps
 *   (`targeting_target_*_workspace_isolation`, vs. the canonical
 *   `target_*_workspace_isolation`). Postgres OR's multiple permissive
 *   policies together, so converting only the canonical name left these
 *   5 tables effectively fail-open regardless — this migration also
 *   drops that duplicate (down() restores it exactly). See
 *   DUPLICATE_TARGETING_POLICIES below.
 *
 * NOT converted this pass (left fail-open, with reasons):
 * ------------------------------------------------------------------
 *   - audit.audit_ledger: workspace_id is NULLABLE BY DESIGN (7,447 of
 *     23,536 live rows are NULL — platform-wide/system audit events),
 *     and PublicApiController::audit() explicitly does
 *     `WHERE workspace_id = ?::uuid OR workspace_id IS NULL` to
 *     deliberately surface those platform rows to any tenant with
 *     workspace access. The canonical strict `workspace_id = GUC` shape
 *     would silently hide all NULL-workspace rows from every caller
 *     forever (NULL never equals anything), which is a product decision
 *     beyond "close the escape hatch" — not attempted here.
 *   - silver.source_trust_scores: has exactly one live reader,
 *     GET /api/v1/admin/source-trust/scores
 *     (admin_tier1_misc.py::list_source_trust_scores) — an admin-only,
 *     service-key-gated endpoint with an OPTIONAL workspace_id filter
 *     that never binds the GUC and, by design, lists across ALL
 *     workspaces when the filter is omitted. Fail-closed would return
 *     zero rows unconditionally (filtered or not), breaking the
 *     intentional cross-workspace admin view. Fixing this needs a
 *     product decision (does this admin view get its own elevated
 *     connection, or does "list all" go away?) beyond this migration's
 *     scope. (services/source_trust/boost.py, the retrieval-ranking
 *     reader, is unwired dead code — not a factor either way.)
 *   - targeting.target_backtests: `workspace_id` is NULLABLE BY DESIGN
 *     in its own creation migration (2026_05_13_100000, unlike its 7
 *     siblings which are all NOT NULL) — modeling cross-workspace model
 *     backtests. Zero live readers and zero rows today, so there's no
 *     concrete regression risk, but converting would foreclose that
 *     nullable design the same way audit_ledger's would; left open
 *     pending the same product decision.
 *
 * Regression coverage: tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php
 * ::test_second_verified_subset_has_no_fail_open_escape_hatch asserts
 * every policy in TABLES below has no escape-hatch clause.
 */
return new class extends Migration
{
    /**
     * @var list<array{schema: string, table: string, policy: string}>
     */
    private const TABLES = [
        ['schema' => 'silver', 'table' => 'hypotheses', 'policy' => 'hypotheses_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'hypothesis_evidence_links', 'policy' => 'hypothesis_evidence_links_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'decision_records', 'policy' => 'decision_records_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'decision_evidence_links', 'policy' => 'decision_evidence_links_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'decision_options', 'policy' => 'decision_options_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'decision_outcomes', 'policy' => 'decision_outcomes_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'decision_lessons_learned', 'policy' => 'decision_lessons_learned_workspace_isolation'],
        ['schema' => 'silver', 'table' => 'saved_map_views', 'policy' => 'saved_map_views_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_recommendations', 'policy' => 'target_recommendations_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_candidate_zones', 'policy' => 'target_candidate_zones_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_scores', 'policy' => 'target_scores_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_score_factors', 'policy' => 'target_score_factors_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_uncertainties', 'policy' => 'target_uncertainties_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_review_decisions', 'policy' => 'target_review_decisions_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_outcomes', 'policy' => 'target_outcomes_workspace_isolation'],
    ];

    /**
     * Live-DB-verified finding (2026-08-15): 5 of the targeting.* tables
     * above carry a SECOND, differently-named permissive policy from
     * 2026_06_03_010000_close_targeting_workflow_rls_gaps, which built its
     * policy name via `str_replace('.', '_', $qualifiedTable).
     * '_workspace_isolation'` (e.g. `targeting_target_recommendations_
     * workspace_isolation`) instead of matching the original policy name
     * from 2026_05_13_100000_create_targeting_schema
     * (`target_recommendations_workspace_isolation`, no schema prefix).
     * That migration's intent was to CLOSE a real IDOR (targeting tables
     * with no RLS at all — see its own docblock), but because Postgres
     * treats multiple permissive policies on the same table as OR'd
     * together, it landed as an ADDITIONAL fail-open policy alongside the
     * original one rather than replacing it.
     *
     * Verified against `georag_test` after a full `migrate` run: without
     * this block, TABLES' policies above flip to fail-closed correctly,
     * but these 5 tables remain effectively fail-open overall — a row
     * satisfying EITHER policy is visible, and the duplicate still has
     * the `IS NULL OR` escape. Converting only the canonical policy name
     * (as 2026_08_14_030000 did, and as this migration's first draft did
     * before this was caught) would have been a silent no-op security fix.
     *
     * @var list<array{schema: string, table: string, policy: string}>
     */
    private const DUPLICATE_TARGETING_POLICIES = [
        ['schema' => 'targeting', 'table' => 'target_recommendations', 'policy' => 'targeting_target_recommendations_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_outcomes', 'policy' => 'targeting_target_outcomes_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_review_decisions', 'policy' => 'targeting_target_review_decisions_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_scores', 'policy' => 'targeting_target_scores_workspace_isolation'],
        ['schema' => 'targeting', 'table' => 'target_candidate_zones', 'policy' => 'targeting_target_candidate_zones_workspace_isolation'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::TABLES as $t) {
            if (! $this->tableExists($t['schema'], $t['table'])) {
                continue;
            }

            // Belt and braces beyond tableExists(): several tables in
            // this list (see class docblock) only get their workspace_id
            // column from phase0 raw SQL in real deployments. The sibling
            // migration 2026_08_15_020000 provisions it for migrate-only
            // environments (CI) — but if that migration is ever skipped
            // or reordered, skip gracefully here rather than repeat the
            // "column workspace_id does not exist" break from 9fac505.
            if (! $this->columnExists($t['schema'], $t['table'], 'workspace_id')) {
                continue;
            }

            $qualified = "{$t['schema']}.{$t['table']}";

            // FORCE ROW LEVEL SECURITY so the policy also binds the table
            // owner (georag, via ad-hoc `psql -U georag` sessions outside
            // Hatchet/pgsql_migrations). Idempotent.
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

        // Drop the duplicate fail-open policies described above — the
        // canonical policy just (re)created for each of these 5 tables
        // already provides full, correct coverage, so the duplicate is
        // pure liability (a second permissive policy that only weakens
        // the first).
        foreach (self::DUPLICATE_TARGETING_POLICIES as $p) {
            if (! $this->tableExists($p['schema'], $p['table'])) {
                continue;
            }
            if (! $this->policyExists($p['schema'], $p['table'], $p['policy'])) {
                continue;
            }
            DB::statement("DROP POLICY IF EXISTS {$p['policy']} ON {$p['schema']}.{$p['table']}");
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // Restore the exact fail-open shape these tables had before this
        // migration.
        foreach (self::TABLES as $t) {
            if (! $this->tableExists($t['schema'], $t['table'])) {
                continue;
            }
            if (! $this->columnExists($t['schema'], $t['table'], 'workspace_id')) {
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

        // Restore the duplicate policy exactly as
        // 2026_06_03_010000_close_targeting_workflow_rls_gaps created it.
        foreach (self::DUPLICATE_TARGETING_POLICIES as $p) {
            if (! $this->tableExists($p['schema'], $p['table'])) {
                continue;
            }
            $qualified = "{$p['schema']}.{$p['table']}";
            DB::statement("DROP POLICY IF EXISTS {$p['policy']} ON {$qualified}");
            DB::statement(<<<SQL
                CREATE POLICY {$p['policy']} ON {$qualified}
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

    private function columnExists(string $schema, string $table, string $column): bool
    {
        return DB::table('information_schema.columns')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->where('column_name', $column)
            ->exists();
    }

    private function policyExists(string $schema, string $table, string $policy): bool
    {
        return DB::table('pg_policies')
            ->where('schemaname', $schema)
            ->where('tablename', $table)
            ->where('policyname', $policy)
            ->exists();
    }
};
