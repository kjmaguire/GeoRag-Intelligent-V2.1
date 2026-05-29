<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create `silver.hypotheses` + `silver.hypothesis_evidence_links`
 * (doc-phase 91 / §9.4).
 *
 * Per master-plan §20.3, every interpretation supports multiple
 * hypotheses, not a single answer. This schema lets the Hypothesis
 * Generator agent (§9.5) emit competing hypotheses that the
 * Interpretation Workspace surfaces for geologist review.
 *
 * Schema is workspace-scoped (RLS via app.workspace_id) — hypotheses
 * are private to the workspace; public-data-only hypotheses still
 * carry workspace_id of the workspace where they were generated.
 *
 * Evidence links are many-to-many between hypotheses and source
 * chunks, with role enum (supporting | contradicting | missing).
 * "missing" rows record evidence-gaps + recommended tests.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.hypotheses (
                hypothesis_id    UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id     UUID         NOT NULL,
                parent_question  TEXT         NOT NULL,
                label            VARCHAR(8)   NOT NULL,
                description      TEXT         NOT NULL,
                confidence       NUMERIC(4,3) NULL,
                confidence_method VARCHAR(40) NULL,
                review_status    VARCHAR(20)  NOT NULL DEFAULT 'ai_suggested',
                reviewed_by_user_id BIGINT    NULL,
                reviewed_at      TIMESTAMPTZ  NULL,
                rationale        TEXT         NULL,
                created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT hypotheses_pkey PRIMARY KEY (hypothesis_id),
                CONSTRAINT hypotheses_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT hypotheses_reviewed_by_user_id_fkey
                    FOREIGN KEY (reviewed_by_user_id)
                    REFERENCES public.users (id)
                    ON DELETE SET NULL,
                CONSTRAINT hypotheses_review_status_valid
                    CHECK (review_status IN ('ai_suggested', 'reviewed', 'accepted', 'rejected')),
                CONSTRAINT hypotheses_confidence_range
                    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
                CONSTRAINT hypotheses_label_format
                    CHECK (label ~ '^[A-Z][A-Z0-9]?$')
            );
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.hypothesis_evidence_links (
                link_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                hypothesis_id    UUID         NOT NULL,
                source_chunk_id  TEXT         NULL,
                role             VARCHAR(20)  NOT NULL,
                weight           NUMERIC(4,3) NULL,
                payload          JSONB        NOT NULL DEFAULT '{}'::jsonb,
                CONSTRAINT hypothesis_evidence_links_pkey PRIMARY KEY (link_id),
                CONSTRAINT hypothesis_evidence_links_hypothesis_id_fkey
                    FOREIGN KEY (hypothesis_id)
                    REFERENCES silver.hypotheses (hypothesis_id)
                    ON DELETE CASCADE,
                CONSTRAINT hypothesis_evidence_links_role_valid
                    CHECK (role IN ('supporting', 'contradicting', 'missing', 'recommended_test')),
                CONSTRAINT hypothesis_evidence_links_weight_range
                    CHECK (weight IS NULL OR (weight >= 0 AND weight <= 1)),
                CONSTRAINT hypothesis_evidence_links_missing_role_chunk_null
                    CHECK (role NOT IN ('missing', 'recommended_test') OR source_chunk_id IS NULL)
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_hypotheses_workspace
             ON silver.hypotheses (workspace_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_hypotheses_parent_question
             ON silver.hypotheses USING HASH (md5(parent_question));'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_hypothesis_evidence_links_hypothesis
             ON silver.hypothesis_evidence_links (hypothesis_id);'
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_hypothesis_evidence_links_chunk
             ON silver.hypothesis_evidence_links (source_chunk_id)
             WHERE source_chunk_id IS NOT NULL;'
        );

        // RLS — Doc-phase 172 DROP-first idempotency.
        DB::statement('ALTER TABLE silver.hypotheses ENABLE ROW LEVEL SECURITY;');
        DB::statement('DROP POLICY IF EXISTS hypotheses_workspace_isolation ON silver.hypotheses;');
        DB::statement(<<<'SQL'
            CREATE POLICY hypotheses_workspace_isolation
                ON silver.hypotheses
                USING (workspace_id::text = current_setting('app.workspace_id', true))
                WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
        SQL);

        DB::statement('ALTER TABLE silver.hypothesis_evidence_links ENABLE ROW LEVEL SECURITY;');
        DB::statement('DROP POLICY IF EXISTS hypothesis_evidence_links_workspace_isolation ON silver.hypothesis_evidence_links;');
        DB::statement(<<<'SQL'
            CREATE POLICY hypothesis_evidence_links_workspace_isolation
                ON silver.hypothesis_evidence_links
                USING (EXISTS (
                    SELECT 1 FROM silver.hypotheses h
                    WHERE h.hypothesis_id = hypothesis_evidence_links.hypothesis_id
                      AND h.workspace_id::text = current_setting('app.workspace_id', true)
                ));
        SQL);

        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.hypotheses TO georag_app;');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.hypothesis_evidence_links TO georag_app;');
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.hypothesis_evidence_links;');
        DB::statement('DROP TABLE IF EXISTS silver.hypotheses;');
    }
};
