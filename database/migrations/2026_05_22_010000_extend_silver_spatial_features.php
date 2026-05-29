<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Extend silver.spatial_features for full generic-vector ingest provenance.
 *
 * silver.spatial_features already exists (2026_04_10_120100) with workspace
 * tenancy + RLS (raw phase0/100-rls block 5). It's the polymorphic landing
 * table for generic GIS vector features (faults, contacts, mineralization
 * zones, etc.) parsed from .shp / .gpkg / .gdb by silver_spatial.py.
 *
 * The 2026-05-22 bronze/silver/gold audit surfaced four gaps versus the
 * "full provenance" contract:
 *
 *   1. No bronze-side join key — source_file is a string filename, not a
 *      hash, so we can't reliably join back to bronze.ingest_manifest.
 *   2. Multi-layer files (.gpkg often holds 10+ layers; .gdb same) — there
 *      was no column for the original layer name inside the container.
 *   3. No CHECK constraint on feature_type, so the parser could land any
 *      string and the agentic-retrieval anomaly subgraph couldn't rely on
 *      a fixed vocabulary.
 *   4. No optional links to an interpretation PDF, no parser-confidence
 *      score, no native CRS retained as an EPSG integer.
 *
 * This migration adds:
 *   - source_file_sha256       char(64), nullable (joins bronze.ingest_manifest)
 *   - source_layer             text, nullable (the layer name inside .gpkg/.gdb)
 *   - source_feature_id        text, nullable (the original FID — round-trip diffing)
 *   - interpretation_pdf_id    uuid, nullable, FK → bronze.source_files
 *   - feature_role             varchar(32), nullable (subdivider within feature_type)
 *   - confidence               real, nullable (parser confidence 0-1)
 *   - crs_epsg_native          int, nullable (original EPSG before transform to 4326)
 *   - feature_type CHECK constraint formalising the 12-value vocabulary
 *
 * All additive — no drops, no NOT NULL on new columns, safe to re-run.
 *
 * SQLite — gated on Postgres (PostGIS-only table).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // workspace_id is added by phase0 raw SQL (100-rls-tenant-isolation-block4.sql)
        // on production. The test DB (georag_test) doesn't run phase0; mirror
        // that step here so the migration is self-healing in either environment.
        DB::statement(<<<'SQL'
            ALTER TABLE silver.spatial_features
                ADD COLUMN IF NOT EXISTS workspace_id          uuid,
                ADD COLUMN IF NOT EXISTS source_file_sha256    char(64),
                ADD COLUMN IF NOT EXISTS source_layer          text,
                ADD COLUMN IF NOT EXISTS source_feature_id     text,
                ADD COLUMN IF NOT EXISTS interpretation_pdf_id uuid,
                ADD COLUMN IF NOT EXISTS feature_role          varchar(32),
                ADD COLUMN IF NOT EXISTS confidence            real,
                ADD COLUMN IF NOT EXISTS crs_epsg_native       integer
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS idx_spatial_features_workspace_id ON silver.spatial_features (workspace_id)');

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.table_constraints
                     WHERE table_schema = 'silver'
                       AND table_name   = 'spatial_features'
                       AND constraint_name = 'spatial_features_interpretation_pdf_fkey'
                ) THEN
                    ALTER TABLE silver.spatial_features
                        ADD CONSTRAINT spatial_features_interpretation_pdf_fkey
                        FOREIGN KEY (interpretation_pdf_id)
                        REFERENCES bronze.source_files (id)
                        ON DELETE SET NULL;
                END IF;
            END $$;
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.spatial_features
                DROP CONSTRAINT IF EXISTS chk_spatial_features_type;
        SQL);
        DB::statement(<<<'SQL'
            ALTER TABLE silver.spatial_features
                ADD CONSTRAINT chk_spatial_features_type
                CHECK (feature_type IN (
                    'fault', 'contact', 'mineralization_zone',
                    'shear_zone', 'dyke', 'alteration_halo',
                    'lineament', 'occurrence', 'sample_point',
                    'outcrop', 'boundary', 'other'
                )) NOT VALID;
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.spatial_features
                DROP CONSTRAINT IF EXISTS chk_spatial_features_confidence;
        SQL);
        DB::statement(<<<'SQL'
            ALTER TABLE silver.spatial_features
                ADD CONSTRAINT chk_spatial_features_confidence
                CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1));
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.spatial_features
                DROP CONSTRAINT IF EXISTS chk_spatial_features_crs_native;
        SQL);
        DB::statement(<<<'SQL'
            ALTER TABLE silver.spatial_features
                ADD CONSTRAINT chk_spatial_features_crs_native
                CHECK (crs_epsg_native IS NULL OR (crs_epsg_native BETWEEN 1024 AND 32767));
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_spatial_features_source_sha
                       ON silver.spatial_features (source_file_sha256)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_spatial_features_workspace_type
                       ON silver.spatial_features (workspace_id, feature_type)');

        DB::statement("COMMENT ON COLUMN silver.spatial_features.source_file_sha256 IS
            'SHA256 of the bronze source file — joins to bronze.ingest_manifest for full provenance.'");
        DB::statement("COMMENT ON COLUMN silver.spatial_features.source_layer IS
            'Original layer name inside the .shp / .gpkg / .gdb. A single GeoPackage / File Geodatabase often holds many layers; we keep the origin.'");
        DB::statement("COMMENT ON COLUMN silver.spatial_features.source_feature_id IS
            'Original FID inside the source layer — kept for round-trip diffing.'");
        DB::statement("COMMENT ON COLUMN silver.spatial_features.feature_role IS
            'Optional subdivider within feature_type — fault: thrust/normal/strike-slip; contact: depositional/intrusive/unconformity; etc.'");
        DB::statement("COMMENT ON COLUMN silver.spatial_features.confidence IS
            'Parser confidence (0-1). NULL when not computed.'");
        DB::statement("COMMENT ON COLUMN silver.spatial_features.crs_epsg_native IS
            'Original EPSG code before transform to 4326. Preserved for ingest QA.'");
        DB::statement("COMMENT ON CONSTRAINT chk_spatial_features_type ON silver.spatial_features IS
            'V1 feature_type vocabulary. NOT VALID — historic rows pre-vocabulary are not validated; new inserts are checked.'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.spatial_features DROP CONSTRAINT IF EXISTS chk_spatial_features_type');
        DB::statement('ALTER TABLE silver.spatial_features DROP CONSTRAINT IF EXISTS chk_spatial_features_confidence');
        DB::statement('ALTER TABLE silver.spatial_features DROP CONSTRAINT IF EXISTS chk_spatial_features_crs_native');
        DB::statement('ALTER TABLE silver.spatial_features DROP CONSTRAINT IF EXISTS spatial_features_interpretation_pdf_fkey');
        DB::statement('DROP INDEX IF EXISTS silver.idx_spatial_features_source_sha');
        DB::statement('DROP INDEX IF EXISTS silver.idx_spatial_features_workspace_type');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.spatial_features
                DROP COLUMN IF EXISTS crs_epsg_native,
                DROP COLUMN IF EXISTS confidence,
                DROP COLUMN IF EXISTS feature_role,
                DROP COLUMN IF EXISTS interpretation_pdf_id,
                DROP COLUMN IF EXISTS source_feature_id,
                DROP COLUMN IF EXISTS source_layer,
                DROP COLUMN IF EXISTS source_file_sha256
        SQL);
    }
};
