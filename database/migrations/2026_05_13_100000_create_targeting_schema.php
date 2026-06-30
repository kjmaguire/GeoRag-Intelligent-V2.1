<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create `targeting.*` schema — 10 tables per master-plan §18.6
 * (doc-phase 85 / §8.1).
 *
 * Tables:
 *   - target_models                 — deposit model templates with weights
 *   - target_model_versions         — A/B versioning for weighted vs XGBoost
 *   - target_candidate_zones        — generated polygons (PostGIS POLYGON)
 *   - target_scores                 — per-zone aggregate score + uncertainty
 *   - target_score_factors          — per-zone, per-factor SHAP-equivalent
 *   - target_uncertainties          — per-factor uncertainty + aggregate
 *   - target_recommendations        — final ranked recommendations
 *   - target_review_decisions       — geologist decisions (R5 sign-off)
 *   - target_outcomes               — post-drilling outcomes (Phase 12 input)
 *   - target_backtests              — per-model performance metrics (Phase 12)
 *
 * All tables RLS-protected via app.workspace_id session setting,
 * same pattern as silver.* phase3 tables and silver.saved_map_views
 * (doc-phase 76).
 *
 * JSONB-heavy payloads (factor_weights, evidence_payload,
 * uncertainty_breakdown, etc.) absorb §8 sub-step tuning without
 * requiring schema migrations.
 *
 * Apply pattern: laravel `georag_app` cannot CREATE in custom schemas;
 * superuser `georag` applies via psql + manual migrations row INSERT.
 * See `scripts/phase6_master_plan_step4_5_verify.sh` for the pattern.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('CREATE SCHEMA IF NOT EXISTS targeting;');
        DB::statement('SET search_path TO targeting, silver, public;');

        // ---------------- target_models ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS targeting.target_models (
                target_model_id     UUID         NOT NULL DEFAULT gen_random_uuid(),
                slug                VARCHAR(80)  NOT NULL,
                display_name        VARCHAR(160) NOT NULL,
                commodity_primary   VARCHAR(40)  NOT NULL,
                commodities_secondary TEXT[]     NOT NULL DEFAULT '{}',
                attributes_payload  JSONB        NOT NULL DEFAULT '{}'::jsonb,
                positive_indicators JSONB        NOT NULL DEFAULT '[]'::jsonb,
                negative_indicators JSONB        NOT NULL DEFAULT '[]'::jsonb,
                analogues_payload   JSONB        NOT NULL DEFAULT '[]'::jsonb,
                recommended_next_data JSONB      NOT NULL DEFAULT '[]'::jsonb,
                created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT target_models_pkey PRIMARY KEY (target_model_id),
                CONSTRAINT target_models_slug_unique UNIQUE (slug),
                CONSTRAINT target_models_slug_format CHECK (slug ~ '^[a-z][a-z0-9_]*$')
            );
        SQL);

        // ---------------- target_model_versions ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS targeting.target_model_versions (
                version_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                target_model_id     UUID         NOT NULL,
                version             INTEGER      NOT NULL,
                scoring_kind        VARCHAR(20)  NOT NULL,
                factor_weights      JSONB        NOT NULL DEFAULT '{}'::jsonb,
                constraint_payload  JSONB        NOT NULL DEFAULT '{}'::jsonb,
                is_active           BOOLEAN      NOT NULL DEFAULT false,
                created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT target_model_versions_pkey PRIMARY KEY (version_id),
                CONSTRAINT target_model_versions_unique UNIQUE (target_model_id, version),
                CONSTRAINT target_model_versions_target_model_id_fkey
                    FOREIGN KEY (target_model_id)
                    REFERENCES targeting.target_models (target_model_id)
                    ON DELETE CASCADE,
                CONSTRAINT target_model_versions_scoring_kind_valid
                    CHECK (scoring_kind IN ('weighted', 'xgboost', 'ensemble'))
            );
        SQL);

        // ---------------- target_candidate_zones ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS targeting.target_candidate_zones (
                zone_id             UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id        UUID         NOT NULL,
                project_id          UUID         NOT NULL,
                target_model_id     UUID         NOT NULL,
                run_id              UUID         NOT NULL,
                zone_geom           geometry(Polygon, 4326) NOT NULL,
                evidence_payload    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT target_candidate_zones_pkey PRIMARY KEY (zone_id),
                CONSTRAINT target_candidate_zones_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT target_candidate_zones_project_id_fkey
                    FOREIGN KEY (project_id)
                    REFERENCES silver.projects (project_id)
                    ON DELETE CASCADE,
                CONSTRAINT target_candidate_zones_target_model_id_fkey
                    FOREIGN KEY (target_model_id)
                    REFERENCES targeting.target_models (target_model_id)
                    ON DELETE CASCADE
            );
        SQL);

        // ---------------- target_scores ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS targeting.target_scores (
                score_id            UUID         NOT NULL DEFAULT gen_random_uuid(),
                zone_id             UUID         NOT NULL,
                workspace_id        UUID         NOT NULL,
                model_version_id    UUID         NOT NULL,
                aggregate_score     NUMERIC(10,4) NOT NULL,
                aggregate_uncertainty NUMERIC(10,4) NULL,
                computed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT target_scores_pkey PRIMARY KEY (score_id),
                CONSTRAINT target_scores_zone_id_fkey
                    FOREIGN KEY (zone_id)
                    REFERENCES targeting.target_candidate_zones (zone_id)
                    ON DELETE CASCADE,
                CONSTRAINT target_scores_model_version_id_fkey
                    FOREIGN KEY (model_version_id)
                    REFERENCES targeting.target_model_versions (version_id)
                    ON DELETE RESTRICT,
                CONSTRAINT target_scores_zone_version_unique
                    UNIQUE (zone_id, model_version_id)
            );
        SQL);

        // ---------------- target_score_factors ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS targeting.target_score_factors (
                factor_id           UUID         NOT NULL DEFAULT gen_random_uuid(),
                score_id            UUID         NOT NULL,
                factor_name         VARCHAR(80)  NOT NULL,
                factor_value        NUMERIC(10,4) NOT NULL,
                factor_weight       NUMERIC(10,4) NOT NULL,
                contribution        NUMERIC(10,4) NOT NULL,
                evidence_chunk_ids  TEXT[]       NOT NULL DEFAULT '{}',
                CONSTRAINT target_score_factors_pkey PRIMARY KEY (factor_id),
                CONSTRAINT target_score_factors_score_id_fkey
                    FOREIGN KEY (score_id)
                    REFERENCES targeting.target_scores (score_id)
                    ON DELETE CASCADE,
                CONSTRAINT target_score_factors_score_factor_unique
                    UNIQUE (score_id, factor_name)
            );
        SQL);

        // ---------------- target_uncertainties ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS targeting.target_uncertainties (
                uncertainty_id      UUID         NOT NULL DEFAULT gen_random_uuid(),
                score_id            UUID         NOT NULL,
                factor_name         VARCHAR(80)  NULL,
                uncertainty_kind    VARCHAR(40)  NOT NULL,
                uncertainty_value   NUMERIC(10,4) NOT NULL,
                method              VARCHAR(40)  NOT NULL,
                payload             JSONB        NOT NULL DEFAULT '{}'::jsonb,
                CONSTRAINT target_uncertainties_pkey PRIMARY KEY (uncertainty_id),
                CONSTRAINT target_uncertainties_score_id_fkey
                    FOREIGN KEY (score_id)
                    REFERENCES targeting.target_scores (score_id)
                    ON DELETE CASCADE,
                CONSTRAINT target_uncertainties_method_valid
                    CHECK (method IN ('bayesian', 'bootstrap', 'analytical', 'heuristic'))
            );
        SQL);

        // ---------------- target_recommendations ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS targeting.target_recommendations (
                recommendation_id   UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id        UUID         NOT NULL,
                project_id          UUID         NOT NULL,
                run_id              UUID         NOT NULL,
                zone_id             UUID         NOT NULL,
                score_id            UUID         NOT NULL,
                rank                INTEGER      NOT NULL,
                explanation_markdown TEXT        NOT NULL,
                created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT target_recommendations_pkey PRIMARY KEY (recommendation_id),
                CONSTRAINT target_recommendations_zone_id_fkey
                    FOREIGN KEY (zone_id)
                    REFERENCES targeting.target_candidate_zones (zone_id)
                    ON DELETE CASCADE,
                CONSTRAINT target_recommendations_score_id_fkey
                    FOREIGN KEY (score_id)
                    REFERENCES targeting.target_scores (score_id)
                    ON DELETE RESTRICT,
                CONSTRAINT target_recommendations_run_rank_unique
                    UNIQUE (run_id, rank),
                CONSTRAINT target_recommendations_rank_positive CHECK (rank > 0)
            );
        SQL);

        // ---------------- target_review_decisions ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS targeting.target_review_decisions (
                decision_id         UUID         NOT NULL DEFAULT gen_random_uuid(),
                recommendation_id   UUID         NOT NULL,
                workspace_id        UUID         NOT NULL,
                qp_user_id          BIGINT       NULL,
                qp_credential_id    VARCHAR(120) NULL,
                credential_verified_at TIMESTAMPTZ NULL,
                target_recommendations_hash BYTEA NULL,
                claim_ledger_hash   BYTEA        NULL,
                decision            VARCHAR(20)  NOT NULL,
                rationale           TEXT         NOT NULL,
                signed_at           TIMESTAMPTZ  NULL,
                qp_signature_method VARCHAR(40)  NULL,
                audit_ledger_id     UUID         NULL,
                CONSTRAINT target_review_decisions_pkey PRIMARY KEY (decision_id),
                CONSTRAINT target_review_decisions_recommendation_id_fkey
                    FOREIGN KEY (recommendation_id)
                    REFERENCES targeting.target_recommendations (recommendation_id)
                    ON DELETE CASCADE,
                CONSTRAINT target_review_decisions_qp_user_id_fkey
                    FOREIGN KEY (qp_user_id)
                    REFERENCES public.users (id)
                    ON DELETE SET NULL,
                CONSTRAINT target_review_decisions_decision_valid
                    CHECK (decision IN ('accepted', 'modified', 'rejected', 'signed_off'))
            );
        SQL);

        // ---------------- target_outcomes ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS targeting.target_outcomes (
                outcome_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                recommendation_id   UUID         NOT NULL,
                workspace_id        UUID         NOT NULL,
                drillhole_collar_id UUID         NULL,
                hit_or_miss         VARCHAR(20)  NOT NULL,
                outcome_payload     JSONB        NOT NULL DEFAULT '{}'::jsonb,
                recorded_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT target_outcomes_pkey PRIMARY KEY (outcome_id),
                CONSTRAINT target_outcomes_recommendation_id_fkey
                    FOREIGN KEY (recommendation_id)
                    REFERENCES targeting.target_recommendations (recommendation_id)
                    ON DELETE CASCADE,
                CONSTRAINT target_outcomes_hit_or_miss_valid
                    CHECK (hit_or_miss IN ('hit', 'miss', 'partial', 'pending', 'unresolvable'))
            );
        SQL);

        // ---------------- target_backtests ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS targeting.target_backtests (
                backtest_id         UUID         NOT NULL DEFAULT gen_random_uuid(),
                model_version_id    UUID         NOT NULL,
                workspace_id        UUID         NULL,
                window_start        TIMESTAMPTZ  NOT NULL,
                window_end          TIMESTAMPTZ  NOT NULL,
                metrics_payload     JSONB        NOT NULL DEFAULT '{}'::jsonb,
                computed_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT target_backtests_pkey PRIMARY KEY (backtest_id),
                CONSTRAINT target_backtests_model_version_id_fkey
                    FOREIGN KEY (model_version_id)
                    REFERENCES targeting.target_model_versions (version_id)
                    ON DELETE CASCADE,
                CONSTRAINT target_backtests_window_valid CHECK (window_end > window_start)
            );
        SQL);

        // ---------------- Indexes ----------------
        DB::statement('CREATE INDEX IF NOT EXISTS idx_target_candidate_zones_workspace_project
                       ON targeting.target_candidate_zones (workspace_id, project_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_target_candidate_zones_run
                       ON targeting.target_candidate_zones (run_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_target_candidate_zones_geom_gist
                       ON targeting.target_candidate_zones USING GIST (zone_geom);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_target_recommendations_run_rank
                       ON targeting.target_recommendations (run_id, rank);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_target_review_decisions_recommendation
                       ON targeting.target_review_decisions (recommendation_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_target_outcomes_recommendation
                       ON targeting.target_outcomes (recommendation_id);');

        // ---------------- RLS ----------------
        $rls_tables = [
            'target_candidate_zones',
            'target_scores',
            'target_score_factors',
            'target_uncertainties',
            'target_recommendations',
            'target_review_decisions',
            'target_outcomes',
        ];

        foreach ($rls_tables as $tbl) {
            DB::statement("ALTER TABLE targeting.{$tbl} ENABLE ROW LEVEL SECURITY;");
        }

        // Doc-phase 172 — DROP-first guards each CREATE POLICY against
        // re-run under RefreshDatabase (custom schemas survive
        // `migrate:fresh` so the policies linger between cycles).

        // target_candidate_zones, target_recommendations, target_review_decisions,
        // target_outcomes have direct workspace_id columns.
        foreach (['target_candidate_zones', 'target_recommendations',
            'target_review_decisions', 'target_outcomes'] as $tbl) {
            DB::statement("DROP POLICY IF EXISTS {$tbl}_workspace_isolation ON targeting.{$tbl};");
            DB::statement(<<<SQL
                CREATE POLICY {$tbl}_workspace_isolation
                    ON targeting.{$tbl}
                    USING (workspace_id::text = current_setting('app.workspace_id', true))
                    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
            SQL);
        }

        // target_scores, target_score_factors, target_uncertainties scope via parent
        // (zone_id → workspace_id). Use a target_scores workspace_id column directly
        // for performance; factors + uncertainties reach via score_id.
        DB::statement('DROP POLICY IF EXISTS target_scores_workspace_isolation ON targeting.target_scores;');
        DB::statement(<<<'SQL'
            CREATE POLICY target_scores_workspace_isolation
                ON targeting.target_scores
                USING (workspace_id::text = current_setting('app.workspace_id', true))
                WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
        SQL);

        // factors + uncertainties: scope via EXISTS subquery on target_scores.
        DB::statement('DROP POLICY IF EXISTS target_score_factors_workspace_isolation ON targeting.target_score_factors;');
        DB::statement(<<<'SQL'
            CREATE POLICY target_score_factors_workspace_isolation
                ON targeting.target_score_factors
                USING (EXISTS (
                    SELECT 1 FROM targeting.target_scores s
                    WHERE s.score_id = target_score_factors.score_id
                      AND s.workspace_id::text = current_setting('app.workspace_id', true)
                ));
        SQL);

        DB::statement('DROP POLICY IF EXISTS target_uncertainties_workspace_isolation ON targeting.target_uncertainties;');
        DB::statement(<<<'SQL'
            CREATE POLICY target_uncertainties_workspace_isolation
                ON targeting.target_uncertainties
                USING (EXISTS (
                    SELECT 1 FROM targeting.target_scores s
                    WHERE s.score_id = target_uncertainties.score_id
                      AND s.workspace_id::text = current_setting('app.workspace_id', true)
                ));
        SQL);

        // ---------------- Grants ----------------
        DB::statement('GRANT USAGE ON SCHEMA targeting TO georag_app;');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON ALL TABLES IN SCHEMA targeting TO georag_app;');
        DB::statement('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA targeting TO georag_app;');
    }

    public function down(): void
    {
        DB::statement('DROP SCHEMA IF EXISTS targeting CASCADE;');
    }
};
