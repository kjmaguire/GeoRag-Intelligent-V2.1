<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create `silver.geological_ontology_terms` + `_synonyms`
 * (doc-phase 90 / §9.1).
 *
 * Per master-plan §20.1, the ontology covers 11 classes:
 *   deposit_model | commodity | lithology | alteration | structure |
 *   mineral_assemblage | host_rock | geological_age | tectonic_setting |
 *   geochemistry | geophysics
 *
 * Plus an extra reference class:
 *   resource_class — CIM standard categories (measured/indicated/inferred)
 *
 * Schema deliberately minimal: term_id + class + canonical_term +
 * optional payload JSONB for class-specific metadata (anomaly thresholds,
 * BGS rock-classification codes, age range bounds, etc.).
 *
 * Synonyms separated so we can store many synonyms per canonical
 * term without bloating the main row.
 *
 * No RLS — the ontology is GLOBAL reference data, not workspace-
 * scoped. Workspace customization happens through a separate overlay
 * table in a later phase if needed.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('SET search_path TO silver, public;');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.geological_ontology_terms (
                term_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                class            VARCHAR(40)  NOT NULL,
                canonical_term   VARCHAR(160) NOT NULL,
                description      TEXT         NULL,
                payload          JSONB        NOT NULL DEFAULT '{}'::jsonb,
                created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT geological_ontology_terms_pkey
                    PRIMARY KEY (term_id),
                CONSTRAINT geological_ontology_terms_class_term_unique
                    UNIQUE (class, canonical_term),
                CONSTRAINT geological_ontology_terms_class_valid
                    CHECK (class IN (
                        'deposit_model',
                        'commodity',
                        'lithology',
                        'alteration',
                        'structure',
                        'mineral_assemblage',
                        'host_rock',
                        'geological_age',
                        'tectonic_setting',
                        'geochemistry',
                        'geophysics',
                        'resource_class'
                    ))
            );
        SQL);

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.geological_ontology_synonyms (
                synonym_id       UUID         NOT NULL DEFAULT gen_random_uuid(),
                term_id          UUID         NOT NULL,
                synonym          VARCHAR(160) NOT NULL,
                language_code    VARCHAR(8)   NOT NULL DEFAULT 'en',
                source           VARCHAR(80)  NULL,
                created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT geological_ontology_synonyms_pkey
                    PRIMARY KEY (synonym_id),
                CONSTRAINT geological_ontology_synonyms_term_id_fkey
                    FOREIGN KEY (term_id)
                    REFERENCES silver.geological_ontology_terms (term_id)
                    ON DELETE CASCADE,
                CONSTRAINT geological_ontology_synonyms_unique
                    UNIQUE (term_id, synonym, language_code)
            );
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_geological_ontology_terms_class
             ON silver.geological_ontology_terms (class);',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_geological_ontology_synonyms_synonym
             ON silver.geological_ontology_synonyms (synonym);',
        );

        DB::statement('GRANT SELECT ON silver.geological_ontology_terms TO georag_app;');
        DB::statement('GRANT SELECT ON silver.geological_ontology_synonyms TO georag_app;');
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS silver.geological_ontology_synonyms;');
        DB::statement('DROP TABLE IF EXISTS silver.geological_ontology_terms;');
    }
};
