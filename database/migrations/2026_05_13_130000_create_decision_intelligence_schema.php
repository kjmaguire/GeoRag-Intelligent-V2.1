<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create `silver.decision_records` + 4 related tables (§9.9 / §21.1-2).
 *
 * Five tables per master-plan §21.2:
 *   - decision_records              — core; one row per decision
 *   - decision_evidence_links       — many-to-many decisions ↔ evidence
 *   - decision_options              — options considered per decision
 *   - decision_outcomes             — post-decision outcome tracking
 *   - decision_lessons_learned      — retrospective captures
 *
 * Eight tracked decision types per §21.3:
 *   target_recommendation | crs_decision | schema_mapping |
 *   public_data_import | export_approval | workflow_enablement |
 *   conflict_resolution | report_signoff
 *
 * Doc-phase 92.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.decision_records (
                decision_id      UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id     UUID         NOT NULL,
                decision_type    VARCHAR(40)  NOT NULL,
                recommendation   TEXT         NOT NULL,
                human_decision   TEXT         NOT NULL,
                reason           TEXT         NULL,
                uncertainty      NUMERIC(4,3) NULL,
                decided_by_user_id BIGINT     NOT NULL,
                decided_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                hash             BYTEA        NULL,
                audit_ledger_id  UUID         NULL,
                CONSTRAINT decision_records_pkey PRIMARY KEY (decision_id),
                CONSTRAINT decision_records_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE CASCADE,
                CONSTRAINT decision_records_decided_by_user_id_fkey
                    FOREIGN KEY (decided_by_user_id)
                    REFERENCES public.users (id)
                    ON DELETE RESTRICT,
                CONSTRAINT decision_records_decision_type_valid
                    CHECK (decision_type IN (
                        'target_recommendation',
                        'crs_decision',
                        'schema_mapping',
                        'public_data_import',
                        'export_approval',
                        'workflow_enablement',
                        'conflict_resolution',
                        'report_signoff'
                    )),
                CONSTRAINT decision_records_uncertainty_range
                    CHECK (uncertainty IS NULL OR (uncertainty >= 0 AND uncertainty <= 1))
            );
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.decision_evidence_links (
                link_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                decision_id      UUID         NOT NULL,
                source_chunk_id  TEXT         NOT NULL,
                role             VARCHAR(20)  NOT NULL DEFAULT 'supporting',
                payload          JSONB        NOT NULL DEFAULT '{}'::jsonb,
                CONSTRAINT decision_evidence_links_pkey PRIMARY KEY (link_id),
                CONSTRAINT decision_evidence_links_decision_id_fkey
                    FOREIGN KEY (decision_id)
                    REFERENCES silver.decision_records (decision_id)
                    ON DELETE CASCADE,
                CONSTRAINT decision_evidence_links_role_valid
                    CHECK (role IN ('supporting', 'contradicting', 'context'))
            );
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.decision_options (
                option_id        UUID         NOT NULL DEFAULT gen_random_uuid(),
                decision_id      UUID         NOT NULL,
                label            VARCHAR(80)  NOT NULL,
                description      TEXT         NOT NULL,
                was_chosen       BOOLEAN      NOT NULL DEFAULT false,
                payload          JSONB        NOT NULL DEFAULT '{}'::jsonb,
                CONSTRAINT decision_options_pkey PRIMARY KEY (option_id),
                CONSTRAINT decision_options_decision_id_fkey
                    FOREIGN KEY (decision_id)
                    REFERENCES silver.decision_records (decision_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.decision_outcomes (
                outcome_id       UUID         NOT NULL DEFAULT gen_random_uuid(),
                decision_id      UUID         NOT NULL,
                outcome_kind     VARCHAR(40)  NOT NULL,
                outcome_payload  JSONB        NOT NULL DEFAULT '{}'::jsonb,
                observed_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT decision_outcomes_pkey PRIMARY KEY (outcome_id),
                CONSTRAINT decision_outcomes_decision_id_fkey
                    FOREIGN KEY (decision_id)
                    REFERENCES silver.decision_records (decision_id)
                    ON DELETE CASCADE
            );
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.decision_lessons_learned (
                lesson_id        UUID         NOT NULL DEFAULT gen_random_uuid(),
                decision_id      UUID         NOT NULL,
                captured_by_user_id BIGINT    NULL,
                lesson_markdown  TEXT         NOT NULL,
                captured_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT decision_lessons_learned_pkey PRIMARY KEY (lesson_id),
                CONSTRAINT decision_lessons_learned_decision_id_fkey
                    FOREIGN KEY (decision_id)
                    REFERENCES silver.decision_records (decision_id)
                    ON DELETE CASCADE,
                CONSTRAINT decision_lessons_learned_captured_by_user_id_fkey
                    FOREIGN KEY (captured_by_user_id)
                    REFERENCES public.users (id)
                    ON DELETE SET NULL
            );
        SQL);

        // Indexes
        DB::statement('CREATE INDEX IF NOT EXISTS idx_decision_records_workspace_type
                       ON silver.decision_records (workspace_id, decision_type);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_decision_records_decided_at
                       ON silver.decision_records (decided_at DESC);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_decision_evidence_links_decision
                       ON silver.decision_evidence_links (decision_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_decision_options_decision
                       ON silver.decision_options (decision_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_decision_outcomes_decision
                       ON silver.decision_outcomes (decision_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_decision_lessons_learned_decision
                       ON silver.decision_lessons_learned (decision_id);');

        // RLS — workspace_id direct on decision_records; via EXISTS for children.
        // Doc-phase 172 DROP-first idempotency.
        DB::statement('ALTER TABLE silver.decision_records ENABLE ROW LEVEL SECURITY;');
        DB::statement('DROP POLICY IF EXISTS decision_records_workspace_isolation ON silver.decision_records;');
        DB::statement(<<<'SQL'
            CREATE POLICY decision_records_workspace_isolation
                ON silver.decision_records
                USING (workspace_id::text = current_setting('app.workspace_id', true))
                WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
        SQL);

        foreach (['decision_evidence_links', 'decision_options',
            'decision_outcomes', 'decision_lessons_learned'] as $tbl) {
            DB::statement("ALTER TABLE silver.{$tbl} ENABLE ROW LEVEL SECURITY;");
            DB::statement("DROP POLICY IF EXISTS {$tbl}_workspace_isolation ON silver.{$tbl};");
            DB::statement(<<<SQL
                CREATE POLICY {$tbl}_workspace_isolation
                    ON silver.{$tbl}
                    USING (EXISTS (
                        SELECT 1 FROM silver.decision_records d
                        WHERE d.decision_id = {$tbl}.decision_id
                          AND d.workspace_id::text = current_setting('app.workspace_id', true)
                    ));
            SQL);
        }

        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.decision_records TO georag_app;');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.decision_evidence_links TO georag_app;');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.decision_options TO georag_app;');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.decision_outcomes TO georag_app;');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.decision_lessons_learned TO georag_app;');
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.decision_lessons_learned;');
        DB::statement('DROP TABLE IF EXISTS silver.decision_outcomes;');
        DB::statement('DROP TABLE IF EXISTS silver.decision_options;');
        DB::statement('DROP TABLE IF EXISTS silver.decision_evidence_links;');
        DB::statement('DROP TABLE IF EXISTS silver.decision_records;');
    }
};
