<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Tests\TestCase;

/**
 * Pin the silver.archive_ingest_runs migration contract.
 *
 * Added with audit item C (2026-06-03) — observability for ZIP
 * archive ingests so they stop silently vanishing on extract crashes.
 * Companion Python test:
 * ``src/fastapi/tests/test_ingest_zip_archive_observability.py``.
 *
 * Strategy mirrors the other tenancy migration tests: file-content
 * assertions so the test runs without a live DB. CI verifies the
 * actual DB shape when `RefreshDatabase` runs the migration.
 */
class ArchiveIngestRunsMigrationTest extends TestCase
{
    private function migrationContents(): string
    {
        $path = base_path(
            'database/migrations/2026_06_03_040000_create_silver_archive_ingest_runs.php',
        );
        $this->assertFileExists(
            $path,
            'Archive ingest runs migration missing. Without it the '
            ._archive_progress_helpers_message()
            .' helpers all no-op and observability is back to zero.',
        );

        return (string) file_get_contents($path);
    }

    public function test_migration_creates_archive_ingest_runs_table(): void
    {
        $contents = $this->migrationContents();

        $this->assertStringContainsString(
            'CREATE TABLE IF NOT EXISTS silver.archive_ingest_runs',
            $contents,
            'Migration must create silver.archive_ingest_runs.',
        );
    }

    public function test_migration_declares_required_columns(): void
    {
        $contents = $this->migrationContents();

        foreach ([
            'archive_run_id        UUID',
            'workspace_id          UUID         NOT NULL',
            'project_id            UUID         NOT NULL',
            'run_id                UUID         NOT NULL',
            'minio_key             TEXT         NOT NULL',
            'status                TEXT         NOT NULL',
            'file_count            INTEGER',
            'files_succeeded       INTEGER      NOT NULL DEFAULT 0',
            'files_failed          INTEGER      NOT NULL DEFAULT 0',
        ] as $required) {
            $this->assertStringContainsString(
                $required,
                $contents,
                "Migration missing required column declaration: {$required}",
            );
        }
    }

    public function test_migration_pins_status_check_constraint(): void
    {
        $contents = $this->migrationContents();

        $this->assertStringContainsString(
            'archive_ingest_runs_status_valid',
            $contents,
            'Migration must pin the status allow-list via a CHECK '
            .'constraint named archive_ingest_runs_status_valid. The Python '
            .'TERMINAL_STATUSES tuple in _archive_progress.py mirrors this; '
            .'drift means a Python mark_terminal call would pass type-checks '
            .'and PostgresCheckViolation-fail at INSERT (same shape as the '
            .'ollama-in-CHECK bug the audit caught in Theme C).',
        );
        foreach (['queued', 'extracting', 'fanning_out', 'completed', 'failed', 'partial', 'cancelled'] as $status) {
            $this->assertStringContainsString(
                "'{$status}'",
                $contents,
                "Status '{$status}' must appear in the CHECK constraint.",
            );
        }
    }

    public function test_migration_enables_rls_and_grants(): void
    {
        $contents = $this->migrationContents();

        $this->assertStringContainsString(
            'ALTER TABLE silver.archive_ingest_runs ENABLE ROW LEVEL SECURITY',
            $contents,
            'archive_ingest_runs must have RLS enabled — without it the '
            .'parent observability surface leaks cross-tenant.',
        );
        $this->assertStringContainsString(
            'archive_ingest_runs_workspace_isolation',
            $contents,
            'Canonical workspace_isolation policy must be installed.',
        );
        $this->assertStringContainsString(
            'GRANT SELECT ON silver.archive_ingest_runs TO georag_read',
            $contents,
            'georag_read must be granted SELECT for the IngestionRuns UI '
            .'to display archive parent rows.',
        );
        $this->assertStringContainsString(
            'GRANT INSERT, UPDATE ON silver.archive_ingest_runs TO georag_write',
            $contents,
            'georag_write must be granted INSERT + UPDATE for the workflow '
            .'code path to write parent rows.',
        );
    }

    public function test_migration_adds_archive_run_id_fk_to_ingest_progress(): void
    {
        $contents = $this->migrationContents();

        $this->assertStringContainsString(
            'ADD COLUMN IF NOT EXISTS archive_run_id UUID',
            $contents,
            'silver.ingest_progress must gain a nullable archive_run_id '
            .'column so per-file child rows link back to the parent archive '
            .'run (operators drill from "archive failed" → "these 4 PDFs '
            .'crashed inside it").',
        );
        $this->assertStringContainsString(
            'ingest_progress_archive_run_id_fkey',
            $contents,
            'archive_run_id must carry an FK to silver.archive_ingest_runs '
            .'so dropping a parent row never leaves orphan child links.',
        );
        $this->assertStringContainsString(
            'ON DELETE SET NULL',
            $contents,
            'FK must use ON DELETE SET NULL — dropping an archive_ingest_runs '
            .'row should not cascade-delete per-file lineage history.',
        );
    }

    public function test_workflow_module_wires_archive_progress(): void
    {
        $path = base_path('src/fastapi/app/hatchet_workflows/ingest_zip_archive.py');
        $this->assertFileExists($path);
        $src = (string) file_get_contents($path);

        $this->assertStringContainsString(
            '_archive_progress',
            $src,
            'ingest_zip_archive.py must reference _archive_progress — without '
            .'it the parent row never gets written and Theme D observability '
            .'is zero.',
        );
        $this->assertStringContainsString(
            'archive_lifecycle',
            $src,
            'Workflow must wrap body in archive_lifecycle context manager so '
            .'unhandled exceptions auto-transition the parent row to failed.',
        );
        $this->assertStringContainsString(
            'on_failure_task',
            $src,
            'Workflow must register an on_failure_task as the second backstop '
            .'for Hatchet cancellations / worker crashes that the body never '
            .'reaches (same pattern as ingest_pdf.on_failure documented in '
            .'cameco-recovery-2026-06-02).',
        );
    }
}

/**
 * Inline helper for error-message readability. Kept private to this file
 * since it's only used in one assertion.
 */
function _archive_progress_helpers_message(): string
{
    return '`_archive_progress`';
}
