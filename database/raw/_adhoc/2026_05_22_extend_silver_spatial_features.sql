-- Manual application of Laravel migration 2026_05_22_010000_extend_silver_spatial_features.
-- Run as the table owner (georag), not via Laravel (georag_app is not an owner-role member).
-- After this script succeeds, mark the migration as applied:
--   INSERT INTO migrations (migration, batch)
--   VALUES ('2026_05_22_010000_extend_silver_spatial_features',
--           (SELECT COALESCE(MAX(batch), 0) + 1 FROM migrations));

ALTER TABLE silver.spatial_features
    ADD COLUMN IF NOT EXISTS source_file_sha256    char(64),
    ADD COLUMN IF NOT EXISTS source_layer          text,
    ADD COLUMN IF NOT EXISTS source_feature_id     text,
    ADD COLUMN IF NOT EXISTS interpretation_pdf_id uuid,
    ADD COLUMN IF NOT EXISTS feature_role          varchar(32),
    ADD COLUMN IF NOT EXISTS confidence            real,
    ADD COLUMN IF NOT EXISTS crs_epsg_native       integer;

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

ALTER TABLE silver.spatial_features
    DROP CONSTRAINT IF EXISTS chk_spatial_features_type;
ALTER TABLE silver.spatial_features
    ADD CONSTRAINT chk_spatial_features_type
    CHECK (feature_type IN (
        'fault', 'contact', 'mineralization_zone',
        'shear_zone', 'dyke', 'alteration_halo',
        'lineament', 'occurrence', 'sample_point',
        'outcrop', 'boundary', 'other'
    )) NOT VALID;

ALTER TABLE silver.spatial_features
    DROP CONSTRAINT IF EXISTS chk_spatial_features_confidence;
ALTER TABLE silver.spatial_features
    ADD CONSTRAINT chk_spatial_features_confidence
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1));

ALTER TABLE silver.spatial_features
    DROP CONSTRAINT IF EXISTS chk_spatial_features_crs_native;
ALTER TABLE silver.spatial_features
    ADD CONSTRAINT chk_spatial_features_crs_native
    CHECK (crs_epsg_native IS NULL OR (crs_epsg_native BETWEEN 1024 AND 32767));

CREATE INDEX IF NOT EXISTS idx_spatial_features_source_sha
    ON silver.spatial_features (source_file_sha256);
CREATE INDEX IF NOT EXISTS idx_spatial_features_workspace_type
    ON silver.spatial_features (workspace_id, feature_type);

COMMENT ON COLUMN silver.spatial_features.source_file_sha256 IS
    'SHA256 of the bronze source file — joins to bronze.ingest_manifest for full provenance.';
COMMENT ON COLUMN silver.spatial_features.source_layer IS
    'Original layer name inside the .shp / .gpkg / .gdb. A single GeoPackage / File Geodatabase often holds many layers; we keep the origin.';
COMMENT ON COLUMN silver.spatial_features.source_feature_id IS
    'Original FID inside the source layer — kept for round-trip diffing.';
COMMENT ON COLUMN silver.spatial_features.feature_role IS
    'Optional subdivider within feature_type — fault: thrust/normal/strike-slip; contact: depositional/intrusive/unconformity; etc.';
COMMENT ON COLUMN silver.spatial_features.confidence IS
    'Parser confidence (0-1). NULL when not computed.';
COMMENT ON COLUMN silver.spatial_features.crs_epsg_native IS
    'Original EPSG code before transform to 4326. Preserved for ingest QA.';
COMMENT ON CONSTRAINT chk_spatial_features_type ON silver.spatial_features IS
    'V1 feature_type vocabulary. NOT VALID — historic rows pre-vocabulary are not validated; new inserts are checked.';

-- Grant the new columns to georag_app so the existing GRANT SELECT/INSERT/UPDATE
-- (raw phase0/100-rls-tenant-isolation-block4.sql) continues to cover them.
-- (Postgres extends table-level grants to new columns automatically — this is
-- a no-op safety net.)
