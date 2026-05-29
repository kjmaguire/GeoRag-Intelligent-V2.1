<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Doc-phase 173 — Phase A ingestion manifest tables.
 *
 * Three tables for the "inspect & partition" pass over large zip
 * archives (the 200GB TIF-bundle being the first real test):
 *
 *   bronze.ingest_runs           — one row per Phase A inspection run;
 *                                  idempotency + progress + summary stats
 *   bronze.ingest_manifest       — one row per file inside the zip;
 *                                  captures path, size, type, file
 *                                  format-specific metadata, and the
 *                                  guessed_project clustering signal
 *   bronze.ingest_triage_samples — OCR'd first-page samples for
 *                                  stratified random subset of files
 *                                  (surfaced in /admin/ingestion-review
 *                                  for SME project-label confirmation)
 *
 * The split keeps the streaming-walk path (manifest writes) decoupled
 * from the OCR sampling path (triage_samples), so a Phase A run can
 * complete the manifest even if OCR fails for some files.
 */
return new class extends Migration
{
    public function up(): void
    {
        // Driver guard — sqlite tests skip cleanly
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('CREATE SCHEMA IF NOT EXISTS bronze');

        // ─────────────────────────── ingest_runs ───────────────────────────
        DB::statement(<<<'SQL'
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
            )
        SQL);

        DB::statement("COMMENT ON TABLE bronze.ingest_runs IS 'One row per Phase A inspection run. Tracks streaming progress + final summary stats for a large-archive walk.'");

        DB::statement('CREATE INDEX IF NOT EXISTS idx_ingest_runs_started ON bronze.ingest_runs (started_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_ingest_runs_status ON bronze.ingest_runs (status, started_at DESC)');

        // ───────────────────────── ingest_manifest ────────────────────────
        DB::statement(<<<'SQL'
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
            )
        SQL);

        DB::statement("COMMENT ON TABLE bronze.ingest_manifest IS 'One row per file inside the inspected archive. Streamed during the Phase A walk; never re-decodes pixels (TIFF metadata only).'");

        DB::statement('CREATE INDEX IF NOT EXISTS idx_manifest_run ON bronze.ingest_manifest (run_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_manifest_cluster ON bronze.ingest_manifest (cluster_key)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_manifest_guessed_project ON bronze.ingest_manifest (guessed_project)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_manifest_file_type ON bronze.ingest_manifest (file_type)');

        // ─────────────────────── ingest_triage_samples ────────────────────
        DB::statement(<<<'SQL'
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
            )
        SQL);

        DB::statement("COMMENT ON TABLE bronze.ingest_triage_samples IS 'OCR samples from stratified random subset of manifest files. SME confirms project labels via /admin/ingestion-review; the confirmed labels become the project_id assignment in Phase B.'");

        DB::statement('CREATE INDEX IF NOT EXISTS idx_triage_run ON bronze.ingest_triage_samples (run_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_triage_cluster ON bronze.ingest_triage_samples (cluster_key)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_triage_unlabeled ON bronze.ingest_triage_samples (run_id) WHERE sme_labeled_at IS NULL');

        // ──────────────────────────── Grants ─────────────────────────────
        DB::statement('GRANT USAGE ON SCHEMA bronze TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE ON bronze.ingest_runs TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE ON bronze.ingest_manifest TO georag_app');
        DB::statement('GRANT SELECT, INSERT, UPDATE ON bronze.ingest_triage_samples TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS bronze.ingest_triage_samples CASCADE');
        DB::statement('DROP TABLE IF EXISTS bronze.ingest_manifest CASCADE');
        DB::statement('DROP TABLE IF EXISTS bronze.ingest_runs CASCADE');
    }
};
