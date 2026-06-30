<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Retrofit RLS policies on newer silver/targeting tables to add the
 * "admin escape hatch" pattern established in the doc-phase 50 RLS
 * migration (database/raw/phase0/95-rls-policies.sql).
 *
 * Doc-phase 129. Cross-references the doc-phase 128 incident where
 * the Eval Dashboard / Decision History admin surfaces couldn't
 * read cross-workspace because the newer RLS policies enforce
 * `workspace_id = current_setting('app.workspace_id', true)`
 * strictly — when the GUC is unset, `text = NULL` evaluates to NULL
 * → policy returns FALSE → 0 rows.
 *
 * Older policies (e.g. silver.low_confidence_page_reviews) use:
 *   USING (
 *     (workspace_id = current_setting('app.workspace_id', true)::uuid)
 *     OR current_setting('app.workspace_id', true) IS NULL
 *     OR current_setting('app.workspace_id', true) = ''
 *   )
 *
 * Result: when the GUC is set, normal workspace isolation; when it's
 * UNSET (admin context), all rows visible. Admin views can read cross-
 * workspace without explicit workspace scoping.
 *
 * This migration retrofits the same pattern on 14 policies across:
 *   - silver.saved_map_views      (doc-phase 76)
 *   - silver.hypotheses            (doc-phase 91)
 *   - silver.decision_records      (doc-phase 92)
 *   - silver.source_trust_scores   (doc-phase 102)
 *   - targeting.target_candidate_zones / target_scores /
 *     target_recommendations / target_review_decisions /
 *     target_outcomes              (doc-phase 85)
 *
 * The EXISTS-based child policies (decision_*, target_score_factors,
 * target_uncertainties, hypothesis_evidence_links, source_trust_features)
 * gain the escape hatch transitively through their parent's policy —
 * no changes needed.
 *
 * Apply pattern: same as doc-phase 76 / 85 / 90 / etc. (georag_app
 * cannot ALTER POLICY; psql -U georag superuser drops + recreates).
 */
return new class extends Migration
{
    /**
     * (workspace_id, policy_name) pairs to retrofit. The new USING
     * clause is identical structure across all 14 (workspace_id on
     * the row vs the GUC + admin escape hatch).
     *
     * @var array<int, array{table: string, policy: string, qualified: string}>
     */
    private array $policies = [
        // doc-phase 76
        ['table' => 'silver.saved_map_views',
            'policy' => 'saved_map_views_workspace_isolation',
            'qualified' => 'silver.saved_map_views'],
        // doc-phase 91
        ['table' => 'silver.hypotheses',
            'policy' => 'hypotheses_workspace_isolation',
            'qualified' => 'silver.hypotheses'],
        // doc-phase 92
        ['table' => 'silver.decision_records',
            'policy' => 'decision_records_workspace_isolation',
            'qualified' => 'silver.decision_records'],
        // doc-phase 102
        ['table' => 'silver.source_trust_scores',
            'policy' => 'source_trust_scores_workspace_isolation',
            'qualified' => 'silver.source_trust_scores'],
        // doc-phase 85 (targeting.*)
        ['table' => 'targeting.target_candidate_zones',
            'policy' => 'target_candidate_zones_workspace_isolation',
            'qualified' => 'targeting.target_candidate_zones'],
        ['table' => 'targeting.target_scores',
            'policy' => 'target_scores_workspace_isolation',
            'qualified' => 'targeting.target_scores'],
        ['table' => 'targeting.target_recommendations',
            'policy' => 'target_recommendations_workspace_isolation',
            'qualified' => 'targeting.target_recommendations'],
        ['table' => 'targeting.target_review_decisions',
            'policy' => 'target_review_decisions_workspace_isolation',
            'qualified' => 'targeting.target_review_decisions'],
        ['table' => 'targeting.target_outcomes',
            'policy' => 'target_outcomes_workspace_isolation',
            'qualified' => 'targeting.target_outcomes'],
    ];

    public function up(): void
    {
        // Doc-phase 157 — RLS policies are PG-only. Skip under sqlite.
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        foreach ($this->policies as $p) {
            // Drop the strict policy
            DB::statement(sprintf(
                'DROP POLICY IF EXISTS %s ON %s',
                $p['policy'],
                $p['qualified'],
            ));

            // Recreate with the admin escape hatch
            DB::statement(sprintf(<<<'SQL'
                CREATE POLICY %s
                    ON %s
                    USING (
                        (workspace_id::text = current_setting('app.workspace_id', true))
                        OR current_setting('app.workspace_id', true) IS NULL
                        OR current_setting('app.workspace_id', true) = ''
                    )
                    WITH CHECK (
                        workspace_id::text = current_setting('app.workspace_id', true)
                    )
                SQL,
                $p['policy'],
                $p['qualified'],
            ));
        }
    }

    public function down(): void
    {
        // Revert to strict policies (without escape hatch)
        foreach ($this->policies as $p) {
            DB::statement(sprintf(
                'DROP POLICY IF EXISTS %s ON %s',
                $p['policy'],
                $p['qualified'],
            ));
            DB::statement(sprintf(<<<'SQL'
                CREATE POLICY %s
                    ON %s
                    USING (workspace_id::text = current_setting('app.workspace_id', true))
                    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true))
                SQL,
                $p['policy'],
                $p['qualified'],
            ));
        }
    }
};
