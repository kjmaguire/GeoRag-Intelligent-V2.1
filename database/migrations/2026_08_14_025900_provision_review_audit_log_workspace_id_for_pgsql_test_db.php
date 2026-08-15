<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB parity gap — silver.review_audit_log has no workspace_id column
 * in the Laravel migration chain (2026_05_24_120000_create_silver_review_queue
 * created it without one; the column only exists in real deployments because
 * database/raw/phase0/97-rls-tenant-isolation-block2.sql's tier_c_tables
 * PL/pgSQL block adds it dynamically as part of the phase0 bootstrap). CI's
 * schema-and-qdrant-drift job runs `php artisan migrate` against a fresh
 * Postgres container with no phase0 bootstrap step, so the column never
 * appears there — 2026_08_14_030000_close_rls_admin_escape_hatch_verified_subset
 * failed with "column workspace_id does not exist" the first time it ran.
 *
 * Mirrors just the column-add step of the phase0 block for this one table
 * (nullable — the RLS policy migration only needs the column to exist, and
 * the table is empty in every migrate-only environment, so there's nothing
 * to backfill). Production already has this column; this is a strict no-op
 * there (guarded by an existence check, matching phase0's own IF-missing
 * check).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        $hasColumn = DB::table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', 'review_audit_log')
            ->where('column_name', 'workspace_id')
            ->exists();

        if ($hasColumn) {
            return;
        }

        DB::statement('ALTER TABLE silver.review_audit_log ADD COLUMN workspace_id uuid');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.review_audit_log DROP COLUMN IF EXISTS workspace_id');
    }
};
