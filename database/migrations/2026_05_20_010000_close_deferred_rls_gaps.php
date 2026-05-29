<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Close the 3 deferred RLS gaps from the Phase 5 RLS audit.
 *
 * Why these 3 were deferred initially
 * -----------------------------------
 * The Phase 5 RLS migration (2026_05_19_180100) closed the gaps that had
 * clean workspace_id → tenant_isolation policies. These three needed
 * non-trivial policies, so they were escalated for design review and
 * land here now per Kyle's approval (2026-05-20).
 *
 * 1. silver.workspaces — the workspaces table itself has workspace_id as
 *    its primary key. The simple tenant_isolation pattern works: when the
 *    `app.workspace_id` GUC is set, the policy restricts visibility to
 *    that row. The original "needs membership-based" concern was based on
 *    a different mental model — a user-id GUC pattern that isn't wired
 *    elsewhere. Use the same fail-open tenant_isolation pattern as the
 *    other silver tables.
 *
 * 2. silver.target_rationales — workspace_id reachable only via
 *    recommendation_id → targeting.target_recommendations join. Use an
 *    EXISTS subquery policy. Same fail-open semantics as
 *    silver.collab_comments which joins anchor_id → silver.collab_anchors.
 *
 * 3. silver.geological_ontology_terms / silver.geological_ontology_synonyms —
 *    cross-workspace reference data. Enable RLS with a permissive SELECT
 *    policy (anyone can read). Writes are application-layer admin-only
 *    via role grants; RLS doesn't need to enforce that.
 *
 * SQLite (test DB) — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // ── silver.workspaces — tenant_isolation matches GUC ─────────────
        DB::statement('ALTER TABLE silver.workspaces ENABLE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY workspaces_tenant_isolation ON silver.workspaces
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        // ── silver.target_rationales — reach workspace via recommendation ──
        DB::statement('ALTER TABLE silver.target_rationales ENABLE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY target_rationales_workspace_isolation ON silver.target_rationales
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR EXISTS (
                  SELECT 1
                  FROM targeting.target_recommendations r
                  WHERE r.recommendation_id = silver.target_rationales.recommendation_id
                    AND r.workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                )
              )
        SQL);

        // ── silver.geological_ontology_* — cross-workspace read-anyone ──
        // RLS is enabled (so we get the safety net + the table is no longer
        // an outlier in the silver schema) but the policy permits SELECT to
        // any session. Write protection comes from role grants — only the
        // admin role can INSERT/UPDATE/DELETE on these tables in production.
        DB::statement('ALTER TABLE silver.geological_ontology_terms ENABLE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY ontology_terms_read_anyone ON silver.geological_ontology_terms
              FOR SELECT USING (true)
        SQL);

        DB::statement('ALTER TABLE silver.geological_ontology_synonyms ENABLE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY ontology_synonyms_read_anyone ON silver.geological_ontology_synonyms
              FOR SELECT USING (true)
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach ([
            ['silver.workspaces', 'workspaces_tenant_isolation'],
            ['silver.target_rationales', 'target_rationales_workspace_isolation'],
            ['silver.geological_ontology_terms', 'ontology_terms_read_anyone'],
            ['silver.geological_ontology_synonyms', 'ontology_synonyms_read_anyone'],
        ] as [$tbl, $policy]) {
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$tbl}");
            DB::statement("ALTER TABLE {$tbl} DISABLE ROW LEVEL SECURITY");
        }
    }
};
