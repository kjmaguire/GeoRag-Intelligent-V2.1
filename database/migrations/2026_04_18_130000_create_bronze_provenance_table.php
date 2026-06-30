<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Sprint 2 parser hardening — per-record audit trail linking silver rows to source files.
 *
 * Enables NI 43-101 compliance reporting ("where did this collar come from?") and supports
 * the column-mapping UI that lands in Sprint 5. One row per silver record, immutable,
 * with source file SHA256, original row number, parser name/version, and ingest run ID.
 * Supports deduplication and traceability across all tabular and document-based ingests.
 */
return new class extends Migration
{
    public function up(): void
    {
        // Create bronze schema if not already present
        DB::statement('CREATE SCHEMA IF NOT EXISTS bronze');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS bronze.provenance (
                provenance_id       UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                target_schema       VARCHAR(32)  NOT NULL,
                target_table        VARCHAR(64)  NOT NULL,
                target_id           UUID         NOT NULL,
                source_file         TEXT         NOT NULL,
                source_file_sha256  CHAR(64)     NOT NULL,
                source_row          INTEGER      NULL,
                source_col_map      JSONB        NULL,
                parser_name         VARCHAR(64)  NOT NULL,
                parser_version      VARCHAR(32)  NOT NULL,
                ingested_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                ingest_run_id       UUID         NULL
            )
        SQL);

        DB::statement("COMMENT ON TABLE bronze.provenance IS 'Immutable audit trail: maps each silver record to its source file, row, parser, and ingest run. Supports compliance reporting and deduplication.'");

        // ── Indexes ──────────────────────────────────────────────────────
        // Primary lookup: find all provenance for a given silver row.
        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_provenance_target
                ON bronze.provenance (target_schema, target_table, target_id)
        ');

        // Deduplication: have we seen this file before?
        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_provenance_sha256
                ON bronze.provenance (source_file_sha256)
        ');

        // Recent-activity queries.
        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_provenance_ingested_at
                ON bronze.provenance (ingested_at DESC)
        ');

        // Partial index: what did this Dagster run ingest?
        DB::statement('
            CREATE INDEX IF NOT EXISTS idx_provenance_ingest_run
                ON bronze.provenance (ingest_run_id)
                WHERE ingest_run_id IS NOT NULL
        ');
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS bronze.provenance CASCADE');
    }
};
