<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 6 (2026-05-22) — OCR Quality Agent state column.
 *
 * Adds silver.document_passages.ocr_status to track the lifecycle of
 * automated OCR quality review. Initial state for every parsed passage
 * is 'accepted' — the Quality Agent (ocr_quality_check_wf) may flip it
 * to 'pending_reocr' when ocr_confidence is below the re-OCR threshold
 * AND a known artifact is detected, or 'low_confidence' when the
 * passage is below the quality threshold but doesn't warrant re-OCR
 * (e.g. cap reached, no artifact pattern matched). After re_ocr_page
 * finishes its retry pass, it sets the status to 'reocr_complete' on
 * the affected passage(s).
 *
 * Enum values:
 *   'accepted'        — default; no quality issue
 *   'pending_reocr'   — flagged + queued for re-OCR
 *   'reocr_complete'  — re-OCR retry pass finished
 *   'low_confidence'  — flagged but not re-OCR'd (informational)
 *
 * Partial index on (workspace_id, document_id, ocr_status) WHERE
 * ocr_status='pending_reocr' so the quality agent can quickly find
 * passages still waiting for retry without scanning the whole table.
 *
 * SQLite (test DB) — gated; column adds are no-ops there. Production
 * silver schema is Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              ADD COLUMN IF NOT EXISTS ocr_status varchar(50) DEFAULT 'accepted'
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN silver.document_passages.ocr_status IS
              'Phase 6 — OCR Quality Agent lifecycle: accepted | pending_reocr | reocr_complete | low_confidence. Default ''accepted''; the quality agent mutates it after the parse commits.'
        SQL);

        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                     WHERE conname = 'document_passages_ocr_status_check'
                ) THEN
                    ALTER TABLE silver.document_passages
                      ADD CONSTRAINT document_passages_ocr_status_check
                      CHECK (ocr_status IS NULL OR ocr_status IN (
                        'accepted',
                        'pending_reocr',
                        'reocr_complete',
                        'low_confidence'
                      ));
                END IF;
            END $$
        SQL);

        DB::statement(<<<'SQL'
            CREATE INDEX IF NOT EXISTS idx_document_passages_pending_reocr
              ON silver.document_passages (workspace_id, document_id, ocr_status)
              WHERE ocr_status = 'pending_reocr'
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement(<<<'SQL'
            DROP INDEX IF EXISTS silver.idx_document_passages_pending_reocr
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              DROP CONSTRAINT IF EXISTS document_passages_ocr_status_check
        SQL);

        DB::statement(<<<'SQL'
            ALTER TABLE silver.document_passages
              DROP COLUMN IF EXISTS ocr_status
        SQL);
    }
};
