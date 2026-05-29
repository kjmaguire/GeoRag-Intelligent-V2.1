<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Plan §1a (entity_aliases table) + §2c (alias_gaps logging + behaviour).
 *
 * entity_aliases: canonical entity ↔ alias mapping per workspace. Lets the
 * pre-retrieval layer resolve "Rowan" → "WRLG Rowan" / "West Red Lake
 * Rowan" / "Rowan gold project" and pass the canonical name into the
 * retrieval filter.
 *
 * alias_gaps: every entity-extraction miss logged for SME review. Feeds
 * the plan §5d feedback loop — high-frequency misses get promoted into
 * entity_aliases on weekly review.
 *
 * RLS: workspace-scoped. NOT applied by overnight run.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // -------------------- entity_aliases --------------------

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.entity_aliases (
                alias_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id      UUID         NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                entity_type       VARCHAR(40)  NOT NULL,
                canonical_name    VARCHAR(255) NOT NULL,
                canonical_uri     TEXT         NULL,
                alias             VARCHAR(255) NOT NULL,
                alias_normalised  VARCHAR(255) NOT NULL,
                confidence        NUMERIC(4,3) NOT NULL DEFAULT 1.000,
                source            VARCHAR(40)  NOT NULL DEFAULT 'sme',
                source_document_id UUID        NULL,
                evidence_text     TEXT         NULL,
                added_by_user_id  BIGINT       NULL
                    REFERENCES public.users(id) ON DELETE SET NULL,
                created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT entity_aliases_pkey PRIMARY KEY (alias_id),

                CONSTRAINT entity_aliases_workspace_alias_unique
                    UNIQUE (workspace_id, entity_type, alias_normalised),

                CONSTRAINT entity_aliases_entity_type_valid
                    CHECK (entity_type IN (
                        'property', 'project', 'company', 'commodity',
                        'hole_id', 'formation', 'document_type',
                        'technical_term', 'mineral', 'method'
                    )),

                CONSTRAINT entity_aliases_source_valid
                    CHECK (source IN (
                        'sme', 'cgi_vocab', 'extracted_from_document',
                        'feedback_loop', 'system'
                    ))
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_entity_aliases_workspace_type_norm
                       ON silver.entity_aliases (workspace_id, entity_type, alias_normalised)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_entity_aliases_canonical
                       ON silver.entity_aliases (workspace_id, entity_type, canonical_name)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_entity_aliases_uri
                       ON silver.entity_aliases (canonical_uri)
                       WHERE canonical_uri IS NOT NULL');

        DB::statement('ALTER TABLE silver.entity_aliases ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.entity_aliases FORCE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY entity_aliases_workspace_isolation
                ON silver.entity_aliases
                USING (
                    workspace_id::text = current_setting('georag.workspace_id', true)
                )
                WITH CHECK (
                    workspace_id::text = current_setting('georag.workspace_id', true)
                )
        SQL);

        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.entity_aliases TO georag_app');

        // -------------------- alias_gaps --------------------

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.alias_gaps (
                gap_id            UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id      UUID         NOT NULL
                    REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                entity_text       VARCHAR(255) NOT NULL,
                entity_text_normalised VARCHAR(255) NOT NULL,
                entity_type_guess VARCHAR(40)  NULL,
                query_id          UUID         NULL,
                user_id           BIGINT       NULL
                    REFERENCES public.users(id) ON DELETE SET NULL,
                detector          VARCHAR(40)  NOT NULL DEFAULT 'entity_resolver',
                created_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                -- Promotion lifecycle: once an SME promotes a gap into
                -- entity_aliases, the promoted_at + promoted_to_alias_id
                -- close the loop.
                promoted_at       TIMESTAMPTZ  NULL,
                promoted_to_alias_id UUID      NULL
                    REFERENCES silver.entity_aliases(alias_id) ON DELETE SET NULL,
                dismissed_at      TIMESTAMPTZ  NULL,
                dismissed_reason  VARCHAR(60)  NULL,

                CONSTRAINT alias_gaps_pkey PRIMARY KEY (gap_id),

                CONSTRAINT alias_gaps_detector_valid
                    CHECK (detector IN (
                        'entity_resolver', 'hole_id_extractor',
                        'commodity_extractor', 'formation_resolver',
                        'fuzzy_match_fallback'
                    )),

                CONSTRAINT alias_gaps_lifecycle_consistent
                    CHECK (
                        (promoted_at IS NULL AND promoted_to_alias_id IS NULL)
                        OR
                        (promoted_at IS NOT NULL AND promoted_to_alias_id IS NOT NULL)
                    )
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_alias_gaps_workspace_norm
                       ON silver.alias_gaps (workspace_id, entity_text_normalised, created_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_alias_gaps_workspace_open
                       ON silver.alias_gaps (workspace_id, created_at DESC)
                       WHERE promoted_at IS NULL AND dismissed_at IS NULL');

        DB::statement('ALTER TABLE silver.alias_gaps ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.alias_gaps FORCE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY alias_gaps_workspace_isolation
                ON silver.alias_gaps
                USING (
                    workspace_id::text = current_setting('georag.workspace_id', true)
                )
                WITH CHECK (
                    workspace_id::text = current_setting('georag.workspace_id', true)
                )
        SQL);

        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON silver.alias_gaps TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.alias_gaps CASCADE');
        DB::statement('DROP TABLE IF EXISTS silver.entity_aliases CASCADE');
    }
};
