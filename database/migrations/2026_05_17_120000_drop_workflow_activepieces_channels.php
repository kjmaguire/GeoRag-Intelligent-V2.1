<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Drop the `workflow.activepieces_channels` table.
 *
 * The Activepieces service was sunset at Phase 3 Step 7 (see
 * `database/raw/phase3/90-activepieces-sunset.sql` — drops the logical
 * DB + role + feature-flag rows). The metadata channel-registry table
 * lingered because the FastAPI admin router still served it; on
 * 2026-05-17 the Phase 0 cleanup pass removed the admin router + Laravel
 * controller methods + frontend page. This migration completes the rip
 * by dropping the now-orphaned table.
 *
 * Kestra replaces Activepieces per the v2.4.2 master plan §1 stack
 * correction. Kestra-side flow metadata lives in its own schema (managed
 * by Kestra itself); GeoRAG does not maintain a parallel channel-registry
 * table.
 *
 * SQLite (test DB) does not have a `workflow` schema so the migration
 * is gated on the Postgres driver.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS workflow.activepieces_channels CASCADE');
    }

    public function down(): void
    {
        // No-op. Re-creating the table without the live service is
        // pointless; the rollback path is documented in
        // database/raw/phase3/90-activepieces-sunset.sql.
    }
};
