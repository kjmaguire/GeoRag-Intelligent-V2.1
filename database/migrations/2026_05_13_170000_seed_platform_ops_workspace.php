<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Doc-phase 133 — seed the `platform_ops` sentinel workspace.
 *
 * Platform-level §21.3 decisions (workflow_enablement of global
 * feature flags, system-wide policy changes, etc.) need a
 * workspace_id to land in `silver.decision_records` because
 * workspace_id is NOT NULL. Real workspaces aren't a good fit for
 * platform-level state.
 *
 * This migration seeds a single sentinel row with a stable UUID so
 * Laravel + FastAPI code can reference it consistently:
 *
 *   workspace_id: f0f0f0f0-0000-0000-0000-000000000001
 *   name:         platform_ops
 *   slug:         platform-ops
 *
 * Idempotent: ON CONFLICT DO NOTHING.
 */
return new class extends Migration
{
    private const PLATFORM_OPS_WORKSPACE_ID = 'f0f0f0f0-0000-0000-0000-000000000001';

    public function up(): void
    {
        // Doc-phase 157 — gate on driver. The `?::uuid` cast + the
        // `silver.workspaces` table only exist on the PG dev DB. SQLite
        // test runs skip the seed entirely (no silver schema there).
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement(
            <<<'SQL'
            INSERT INTO silver.workspaces (workspace_id, name, slug)
            VALUES (?::uuid, ?, ?)
            ON CONFLICT (workspace_id) DO NOTHING
            SQL,
            [
                self::PLATFORM_OPS_WORKSPACE_ID,
                'platform_ops',
                'platform-ops',
            ],
        );
    }

    public function down(): void
    {
        // Leave the sentinel in place — anything that references it
        // would break. Removal is a deliberate operator action.
    }
};
