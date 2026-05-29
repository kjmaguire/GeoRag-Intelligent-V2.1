-- Doc-phase 173 — Phase A ingestion manifest tables.
--
-- Applied via georag superuser directly (Laravel migration role
-- `georag_app` lacks CREATE-on-database). The Laravel migration file
-- at database/migrations/2026_05_14_130000_create_bronze_ingest_manifest.php
-- is the authoritative source — this raw SQL exists for the bootstrap
-- path only.

CREATE SCHEMA IF NOT EXISTS bronze;

-- ─────────────────────────── ingest_runs ───────────────────────────
CREATE TABLE IF NOT EXISTS bronze.ingest_runs (
    run_id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    source_path         TEXT         NOT NULL,
    source_size_bytes   BIGINT       NULL,
    started_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at        TIMESTAMPTZ  NULL,
    status              VARCHAR(20)  NOT NULL DEFAULT 'running',
    files_seen          INTEGER      NOT NULL DEFAULT 0,
    files_indexed       INTEGER      NOT NULL DEFAULT 0,
    files_skipped       INTEGER      NOT NULL DEFAULT 0,
    bytes_seen          BIGINT       NOT NULL DEFAULT 0,
    error_text          TEXT         NULL,
    summary_payload     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ingest_runs_status_valid
        CHECK (status IN ('running', 'completed', 'failed', 'cancelled'))
);

COMMENT ON TABLE bronze.ingest_runs IS 'One row per Phase A inspection run. Tracks streaming progress + final summary stats for a large-archive walk.';

CREATE INDEX IF NOT EXISTS idx_ingest_runs_started ON bronze.ingest_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_status ON bronze.ingest_runs (status, started_at DESC);

-- ───────────────────────── ingest_manifest ────────────────────────
CREATE TABLE IF NOT EXISTS bronze.ingest_manifest (
    manifest_id         UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID         NOT NULL,
    outer_zip_path      TEXT         NOT NULL,
    inner_zip_path      TEXT         NULL,
    file_path_in_zip    TEXT         NOT NULL,
    file_name           TEXT         NOT NULL,
    file_size_bytes     BIGINT       NOT NULL,
    file_type           VARCHAR(32)  NOT NULL,
    file_extension      VARCHAR(16)  NULL,
    tiff_width          INTEGER      NULL,
    tiff_height         INTEGER      NULL,
    tiff_pages          INTEGER      NULL,
    tiff_compression    VARCHAR(32)  NULL,
    tiff_bits_per_pixel INTEGER      NULL,
    tiff_dpi_x          INTEGER      NULL,
    tiff_dpi_y          INTEGER      NULL,
    guessed_project     TEXT         NULL,
    cluster_key         TEXT         NULL,
    indexed_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    anomalies           JSONB        NOT NULL DEFAULT '[]'::jsonb,
    CONSTRAINT ingest_manifest_run_fkey
        FOREIGN KEY (run_id) REFERENCES bronze.ingest_runs (run_id)
        ON DELETE CASCADE
);

COMMENT ON TABLE bronze.ingest_manifest IS 'One row per file inside the inspected archive. Streamed during the Phase A walk; never re-decodes pixels (TIFF metadata only).';

CREATE INDEX IF NOT EXISTS idx_manifest_run ON bronze.ingest_manifest (run_id);
CREATE INDEX IF NOT EXISTS idx_manifest_cluster ON bronze.ingest_manifest (cluster_key);
CREATE INDEX IF NOT EXISTS idx_manifest_guessed_project ON bronze.ingest_manifest (guessed_project);
CREATE INDEX IF NOT EXISTS idx_manifest_file_type ON bronze.ingest_manifest (file_type);

-- ─────────────────────── ingest_triage_samples ────────────────────
CREATE TABLE IF NOT EXISTS bronze.ingest_triage_samples (
    sample_id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id              UUID         NOT NULL,
    manifest_id         UUID         NOT NULL,
    cluster_key         TEXT         NOT NULL,
    ocr_text            TEXT         NULL,
    ocr_confidence      NUMERIC(5,2) NULL,
    ocr_engine          VARCHAR(32)  NULL,
    thumbnail_path      TEXT         NULL,
    detected_language   VARCHAR(8)   NULL,
    inferred_doc_type   VARCHAR(40)  NULL,
    inferred_project    TEXT         NULL,
    sample_taken_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    sme_label_project   TEXT         NULL,
    sme_label_doc_type  VARCHAR(40)  NULL,
    sme_labeled_at      TIMESTAMPTZ  NULL,
    sme_labeled_by_user_id BIGINT    NULL,
    CONSTRAINT triage_run_fkey
        FOREIGN KEY (run_id) REFERENCES bronze.ingest_runs (run_id)
        ON DELETE CASCADE,
    CONSTRAINT triage_manifest_fkey
        FOREIGN KEY (manifest_id) REFERENCES bronze.ingest_manifest (manifest_id)
        ON DELETE CASCADE,
    CONSTRAINT triage_user_fkey
        FOREIGN KEY (sme_labeled_by_user_id) REFERENCES public.users (id)
        ON DELETE SET NULL
);

COMMENT ON TABLE bronze.ingest_triage_samples IS 'OCR samples from stratified random subset of manifest files. SME confirms project labels via /admin/ingestion-review; the confirmed labels become the project_id assignment in Phase B.';

CREATE INDEX IF NOT EXISTS idx_triage_run ON bronze.ingest_triage_samples (run_id);
CREATE INDEX IF NOT EXISTS idx_triage_cluster ON bronze.ingest_triage_samples (cluster_key);
CREATE INDEX IF NOT EXISTS idx_triage_unlabeled ON bronze.ingest_triage_samples (run_id) WHERE sme_labeled_at IS NULL;

-- ──────────────────────────── Grants ─────────────────────────────
GRANT USAGE ON SCHEMA bronze TO georag_app;
GRANT SELECT, INSERT, UPDATE ON bronze.ingest_runs TO georag_app;
GRANT SELECT, INSERT, UPDATE ON bronze.ingest_manifest TO georag_app;
GRANT SELECT, INSERT, UPDATE ON bronze.ingest_triage_samples TO georag_app;

-- Record this raw-SQL apply in the Laravel migrations table so
-- `php artisan migrate` won't try to re-apply the file-based migration.
INSERT INTO migrations (migration, batch)
SELECT '2026_05_14_130000_create_bronze_ingest_manifest',
       COALESCE((SELECT max(batch) FROM migrations), 0) + 1
WHERE NOT EXISTS (
    SELECT 1 FROM migrations
    WHERE migration = '2026_05_14_130000_create_bronze_ingest_manifest'
);
