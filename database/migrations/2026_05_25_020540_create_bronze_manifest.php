<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 1 of the ingestion reliability spec — create bronze.manifest.
 *
 * One row per uploaded source file. Written synchronously by UploadController
 * BEFORE the Hatchet dispatch fires, so the nightly Tier 1 integrity sweep can
 * detect bronze orphans (manifest rows whose paired silver.reports row never
 * landed because the ingest workflow crashed or got cancelled).
 *
 * Unique key is (workspace_id, file_key), not sha256 — the Laravel
 * UploadController timestamps the file_key (e.g.
 * "reports/{projectId}/20260524_212630_BTU_RMG_TechReport.pdf") so a re-upload
 * of the same content gets a new key and re-triggers ingestion. sha256
 * uniqueness would block legitimate force-reingest of identical content.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // bronze schema may not exist on test DBs that haven't run the
        // phase0 raw SQL — create it inline (matches the convention in
        // 2026_04_18_130000_create_bronze_provenance_table.php).
        DB::statement('CREATE SCHEMA IF NOT EXISTS bronze');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS bronze.manifest (
                file_key             text          NOT NULL,
                workspace_id         uuid          NOT NULL,
                sha256               text          NOT NULL,
                document_type        text          NOT NULL,
                uploaded_at          timestamptz   NOT NULL DEFAULT now(),
                ingest_dispatched_at timestamptz,
                ingest_status        text,
                ingest_run_id        uuid,
                last_dispatch_at     timestamptz,
                dispatch_attempts    integer       NOT NULL DEFAULT 0,
                locked_until         timestamptz,
                cancelled_at         timestamptz,
                CONSTRAINT bronze_manifest_workspace_file_key_uq
                    UNIQUE (workspace_id, file_key),
                CONSTRAINT bronze_manifest_ingest_status_valid
                    CHECK (ingest_status IS NULL OR ingest_status IN (
                        'completed','failed','cancelled','timed_out'
                    ))
            )
        SQL);

        // Orphan detection. The nightly Tier 1 sweep filters by this exact
        // predicate — partial index keeps the working set small.
        DB::statement(
            "CREATE INDEX IF NOT EXISTS bronze_manifest_orphan_idx
             ON bronze.manifest (workspace_id, uploaded_at)
             WHERE ingest_status IS NULL
                OR ingest_status NOT IN ('completed','failed','cancelled','timed_out')",
        );

        // Recovery scheduler claims rows by file_key + locked_until check —
        // index gives us O(log n) lookup per claim attempt.
        DB::statement(
            'CREATE INDEX IF NOT EXISTS bronze_manifest_locked_until_idx
             ON bronze.manifest (workspace_id, locked_until)
             WHERE locked_until IS NOT NULL',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS bronze.manifest');
    }
};
