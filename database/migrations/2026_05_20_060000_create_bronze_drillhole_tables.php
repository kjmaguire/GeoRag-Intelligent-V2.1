<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drillhole schema — Bronze tier.
 *
 * Append-only landing tables for raw drillhole data exactly as it
 * arrives from the lab / driller / surveyor. No RLS — these are
 * accessed only by internal pipeline workers (Dagster bronze→silver
 * assets) and never directly by tenant queries. workspace_id is
 * still tracked so downstream silver assets can scope their UPSERTs.
 *
 * Tables created:
 *   bronze.source_files                — root of every provenance chain
 *   bronze.raw_assay_submissions       — lab CSV/Excel exports, as received
 *   bronze.raw_lithology_logs          — field-log entries before code standardisation
 *   bronze.raw_surveys                 — downhole survey shots as measured
 *   bronze.raw_geophysical_runs        — downhole geophysics file metadata
 *   bronze.raw_collar_entries          — collar data before coord/CRS validation
 *   bronze.raw_qaqc_submissions        — QA/QC samples as returned from lab
 *
 * Contract: every row preserves the original CSV row in `raw_row` JSONB
 * so the silver-tier transform can be replayed/audited at any time.
 *
 * SQLite (test DB) — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // ── source_files (root of provenance) ─────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS bronze.source_files (
                id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id        uuid NOT NULL,
                seaweedfs_key       text NOT NULL UNIQUE,
                original_filename   text NOT NULL,
                file_sha256         text NOT NULL,
                file_size_bytes     bigint,
                mime_type           text,
                source_type         text NOT NULL,
                data_type           text,
                campaign_id         uuid,
                ingested_by         uuid,
                ingested_at         timestamptz NOT NULL DEFAULT now(),
                UNIQUE (workspace_id, file_sha256)
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_source_files_workspace_idx ON bronze.source_files (workspace_id)');

        // ── raw_assay_submissions ─────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS bronze.raw_assay_submissions (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                campaign_id     uuid,
                source_file_id  uuid NOT NULL REFERENCES bronze.source_files(id),
                seaweedfs_key   text NOT NULL,
                lab_name        text,
                certificate_ref text,
                sample_id       text NOT NULL,
                hole_id         text,
                from_depth      numeric,
                to_depth        numeric,
                element         text NOT NULL,
                value           numeric,
                unit            text,
                detection_limit numeric,
                over_detection  boolean DEFAULT false,
                under_detection boolean DEFAULT false,
                raw_row         jsonb NOT NULL,
                imported_at     timestamptz NOT NULL DEFAULT now(),
                import_batch_id uuid NOT NULL
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_raw_assay_workspace_idx ON bronze.raw_assay_submissions (workspace_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_raw_assay_batch_idx ON bronze.raw_assay_submissions (import_batch_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_raw_assay_source_idx ON bronze.raw_assay_submissions (source_file_id)');

        // ── raw_lithology_logs ────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS bronze.raw_lithology_logs (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                source_file_id  uuid REFERENCES bronze.source_files(id),
                hole_id         text NOT NULL,
                from_depth      numeric NOT NULL,
                to_depth        numeric NOT NULL,
                rock_name       text,
                description     text,
                colour          text,
                grain_size      text,
                raw_row         jsonb NOT NULL,
                logged_by       text,
                logged_date     date,
                imported_at     timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_raw_lith_workspace_idx ON bronze.raw_lithology_logs (workspace_id)');

        // ── raw_surveys ───────────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS bronze.raw_surveys (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                hole_id         text NOT NULL,
                depth           numeric NOT NULL,
                azimuth         numeric,
                dip             numeric,
                tool_type       text,
                magnetic_field  numeric,
                dip_accuracy    numeric,
                raw_row         jsonb NOT NULL,
                surveyed_at     date,
                imported_at     timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_raw_surveys_workspace_idx ON bronze.raw_surveys (workspace_id)');

        // ── raw_geophysical_runs ──────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS bronze.raw_geophysical_runs (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                hole_id         text NOT NULL,
                run_type        text NOT NULL,
                tool_name       text,
                contractor      text,
                survey_date     date,
                seaweedfs_key   text NOT NULL,
                file_format     text,
                depth_from      numeric,
                depth_to        numeric,
                sample_interval numeric,
                imported_at     timestamptz NOT NULL DEFAULT now(),
                raw_header      jsonb
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_raw_geophys_workspace_idx ON bronze.raw_geophysical_runs (workspace_id)');

        // ── raw_collar_entries ────────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS bronze.raw_collar_entries (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                hole_id         text NOT NULL,
                easting         numeric,
                northing        numeric,
                elevation       numeric,
                azimuth         numeric,
                dip             numeric,
                total_depth     numeric,
                crs_as_entered  text,
                datum           text,
                drill_type      text,
                purpose         text,
                start_date      date,
                end_date        date,
                driller         text,
                raw_row         jsonb NOT NULL,
                imported_at     timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_raw_collar_workspace_idx ON bronze.raw_collar_entries (workspace_id)');

        // ── raw_qaqc_submissions ──────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS bronze.raw_qaqc_submissions (
                id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id    uuid NOT NULL,
                sample_id       text NOT NULL,
                qaqc_type       text NOT NULL,
                standard_ref    text,
                expected_value  numeric,
                expected_unit   text,
                reported_value  numeric,
                reported_unit   text,
                element         text NOT NULL,
                certificate_ref text,
                raw_row         jsonb NOT NULL,
                imported_at     timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS bronze_raw_qaqc_workspace_idx ON bronze.raw_qaqc_submissions (workspace_id)');

        // Documentation comments
        DB::statement("COMMENT ON TABLE bronze.source_files IS 'Root of every drillhole-data provenance chain. One row per ingested file. UNIQUE on (workspace_id, file_sha256) gives natural dedupe across re-uploads.'");
        DB::statement("COMMENT ON TABLE bronze.raw_assay_submissions IS 'Lab assay rows as imported. Append-only; if a lab reissues a certificate, INSERT a new row — do NOT UPDATE. The silver.assays_v2 transform reconciles via certificate_ref + sample_id.'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        // Drop in reverse-dependency order. source_files last because the
        // assay/lithology tables reference it.
        DB::statement('DROP TABLE IF EXISTS bronze.raw_qaqc_submissions');
        DB::statement('DROP TABLE IF EXISTS bronze.raw_collar_entries');
        DB::statement('DROP TABLE IF EXISTS bronze.raw_geophysical_runs');
        DB::statement('DROP TABLE IF EXISTS bronze.raw_surveys');
        DB::statement('DROP TABLE IF EXISTS bronze.raw_lithology_logs');
        DB::statement('DROP TABLE IF EXISTS bronze.raw_assay_submissions');
        DB::statement('DROP TABLE IF EXISTS bronze.source_files');
    }
};
