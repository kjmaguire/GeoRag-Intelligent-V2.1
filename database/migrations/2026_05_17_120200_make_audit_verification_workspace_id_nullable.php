<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * `audit.audit_ledger_verification_runs.workspace_id` was NOT NULL but
 * the canonical `audit.run_verification()` function inserts a verification
 * run row WITHOUT a workspace_id (the verifier walks the global ledger
 * within a time window — workspace scope is incidental, not foundational).
 *
 * Drop the NOT NULL so the verifier can run cleanly. Callers that want
 * workspace-scoped runs still set the column explicitly via the
 * `audit.run_verification_for_workspace()` overload.
 *
 * Skipped under SQLite (test DB has no `audit` schema partitioning).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement(
            'ALTER TABLE audit.audit_ledger_verification_runs '
            .'ALTER COLUMN workspace_id DROP NOT NULL',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        // Reverse is unsafe — there may be NULL rows accumulated since the
        // forward migration. No-op on rollback.
    }
};
