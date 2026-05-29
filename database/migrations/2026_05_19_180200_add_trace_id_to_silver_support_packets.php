<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Add `trace_id` to `silver.support_packets`.
 *
 * The Support Packet Agent (`src/fastapi/app/agents/phase0/support_packet.py`)
 * already accepts a `trace_id` parameter and uses it to fetch the Tempo
 * trace JSON that goes into the assembled bundle. The trace_id was just
 * never persisted on the row itself, so post-incident "show me the
 * support packet for trace X" queries had to scan the manifest JSONB.
 *
 * Storing it as a top-level column lets the trace_id be (a) indexed for
 * cross-store correlation against `workflow.workflow_runs.trace_id` and
 * Langfuse traces, and (b) projected cheaply into the support cockpit
 * UI without unpacking the manifest.
 *
 * Nullable because legacy rows pre-date this column. The agent code is
 * updated in the same change to start writing it.
 *
 * SQLite (test DB) does not have a `silver` schema — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('ALTER TABLE silver.support_packets ADD COLUMN IF NOT EXISTS trace_id text');
        DB::statement(
            'CREATE INDEX IF NOT EXISTS support_packets_trace_id_idx '
            .'ON silver.support_packets (trace_id) WHERE trace_id IS NOT NULL',
        );
        DB::statement(
            'COMMENT ON COLUMN silver.support_packets.trace_id IS '
            ."'W3C Trace Context trace_id — same value appears in Tempo and "
            ."workflow.workflow_runs.trace_id; lets the cockpit jump from packet → trace.'",
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }
        DB::statement('DROP INDEX IF EXISTS silver.support_packets_trace_id_idx');
        DB::statement('ALTER TABLE silver.support_packets DROP COLUMN IF EXISTS trace_id');
    }
};
