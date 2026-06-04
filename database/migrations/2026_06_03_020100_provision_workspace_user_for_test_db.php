<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * Sibling provisioning for `workspace_user` against the sqlite test DB.
 *
 * The companion migration `2026_06_03_020000_create_workspace_user_table.php`
 * creates the table with a cross-schema FK to `silver.workspaces` and a
 * Postgres CHECK constraint on `role`, both of which sqlite doesn't
 * support. The companion migration skips those clauses on sqlite, but
 * the table itself IS created on both backends (Schema::create works
 * everywhere).
 *
 * This sibling exists for parity with the project's "raw-SQL features
 * skipped on sqlite get a *_for_test_db.php mirror that's a no-op on
 * pgsql" convention (see [[test-db-parity-gap]] memory). Right now the
 * table shape is identical across both backends — no test-DB-only
 * structural drift to provision. The file exists as a placeholder so
 * the chain stays clean and any future test-DB-specific reconciliation
 * has an obvious home.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            // No-op on Postgres — the companion migration handled
            // everything. Listed for chain visibility.
            return;
        }

        // sqlite — verify the table exists. The companion migration
        // already created it via Schema::create which works on sqlite.
        // If a future sqlite-specific quirk appears, add the
        // reconciliation here.
        if (! Schema::hasTable('workspace_user')) {
            throw new RuntimeException(
                'workspace_user table missing on sqlite — companion '
                .'migration 2026_06_03_020000 should have created it.',
            );
        }
    }

    public function down(): void
    {
        // Companion migration handles the drop.
    }
};
