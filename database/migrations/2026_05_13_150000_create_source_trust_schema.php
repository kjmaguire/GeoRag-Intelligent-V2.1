<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create `silver.source_trust_scores` + `silver.source_trust_features`
 * (doc-phase 102 / §12.7 + §21.5).
 *
 * Per master plan §21.5, source trust is a Phase 12 deliverable using
 * XGBoost + SHAP. Trust scores feed retrieval ranking + surface in
 * Trust Inspector.
 *
 * Features per §21.5:
 *   - citation accuracy
 *   - claim ledger consistency
 *   - recency
 *   - document type
 *   - author/issuer reputation
 *
 * Workspace-scoped via RLS. Cross-workspace source trust (when
 * applicable for public-source sharing) handled via a future overlay
 * (out of v1 scope).
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.source_trust_scores (
                trust_score_id   UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id     UUID         NOT NULL,
                source_document_id UUID       NOT NULL,
                trust_score      NUMERIC(4,3) NOT NULL,
                model_version    VARCHAR(40)  NOT NULL,
                computed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT source_trust_scores_pkey PRIMARY KEY (trust_score_id),
                CONSTRAINT source_trust_scores_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT source_trust_scores_unique
                    UNIQUE (workspace_id, source_document_id, model_version),
                CONSTRAINT source_trust_scores_range
                    CHECK (trust_score >= 0 AND trust_score <= 1)
            );
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.source_trust_features (
                feature_id       UUID         NOT NULL DEFAULT gen_random_uuid(),
                trust_score_id   UUID         NOT NULL,
                feature_name     VARCHAR(60)  NOT NULL,
                feature_value    NUMERIC(8,4) NOT NULL,
                shap_contribution NUMERIC(8,4) NULL,
                payload          JSONB        NOT NULL DEFAULT '{}'::jsonb,
                CONSTRAINT source_trust_features_pkey PRIMARY KEY (feature_id),
                CONSTRAINT source_trust_features_trust_score_id_fkey
                    FOREIGN KEY (trust_score_id)
                    REFERENCES silver.source_trust_scores (trust_score_id)
                    ON DELETE CASCADE,
                CONSTRAINT source_trust_features_score_feature_unique
                    UNIQUE (trust_score_id, feature_name),
                CONSTRAINT source_trust_features_name_valid
                    CHECK (feature_name IN (
                        'citation_accuracy',
                        'claim_ledger_consistency',
                        'recency',
                        'document_type',
                        'author_issuer_reputation'
                    ))
            );
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_source_trust_scores_workspace
                       ON silver.source_trust_scores (workspace_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_source_trust_scores_source
                       ON silver.source_trust_scores (source_document_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_source_trust_features_trust_score
                       ON silver.source_trust_features (trust_score_id);');

        // Doc-phase 172 — DROP-first idempotency.
        DB::statement('ALTER TABLE silver.source_trust_scores ENABLE ROW LEVEL SECURITY;');
        DB::statement('DROP POLICY IF EXISTS source_trust_scores_workspace_isolation ON silver.source_trust_scores;');
        DB::statement(<<<'SQL'
            CREATE POLICY source_trust_scores_workspace_isolation
                ON silver.source_trust_scores
                USING (workspace_id::text = current_setting('app.workspace_id', true))
                WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
        SQL);

        DB::statement('ALTER TABLE silver.source_trust_features ENABLE ROW LEVEL SECURITY;');
        DB::statement('DROP POLICY IF EXISTS source_trust_features_workspace_isolation ON silver.source_trust_features;');
        DB::statement(<<<'SQL'
            CREATE POLICY source_trust_features_workspace_isolation
                ON silver.source_trust_features
                USING (EXISTS (
                    SELECT 1 FROM silver.source_trust_scores s
                    WHERE s.trust_score_id = source_trust_features.trust_score_id
                      AND s.workspace_id::text = current_setting('app.workspace_id', true)
                ));
        SQL);

        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.source_trust_scores TO georag_app;');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.source_trust_features TO georag_app;');
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.source_trust_features;');
        DB::statement('DROP TABLE IF EXISTS silver.source_trust_scores;');
    }
};
