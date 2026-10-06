<?php

declare(strict_types=1);

namespace Database\Seeders;

use Illuminate\Database\Seeder;
use Illuminate\Support\Facades\DB;

/**
 * Seed default rows in `workspace.agent_timeouts` for the Phase 0
 * agents that exist in src/fastapi/app/agents/phase0 (the vLLM Security Check and
 * GPU/VRAM Health agents were retired with the GPU stack and are no longer
 * seeded). Kickoff §Step 5.3 mandates these defaults exist before any
 * agent invocation runs, so the operational-contract wrapper can read
 * a timeout policy on first call.
 *
 * Defaults (per kickoff): soft=30s, hard=120s, retry=1,
 * scope=workspace, failure_threshold=5, cool_down=300s.
 *
 * Idempotent — ON CONFLICT DO NOTHING preserves operator-tuned values.
 * Skipped on SQLite (test DB has no `workspace` schema).
 */
class Phase0AgentTimeoutsSeeder extends Seeder
{
    public function run(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        $agents = [
            ['Tenant Isolation Auditor', 'R0'],
            ['Lineage Reporter Agent', 'R0'],
            ['Storage Tiering Agent', 'R2'],
            ['Index Health Agent', 'R0'],
            ['Store Reconciliation Agent', 'R2'],
            ['Model Upgrade Watch Agent', 'R0'],
            ['Model Cost Summary Agent', 'R0'],
            ['LLM Incident Diagnosis Agent', 'R0'],
            ['Support Packet Agent', 'R0'],
        ];

        foreach ($agents as [$name, $tier]) {
            DB::statement(
                'INSERT INTO workspace.agent_timeouts
                    (agent_name, risk_tier, soft_timeout_ms, hard_timeout_ms,
                     retry_count, circuit_breaker_scope, failure_threshold,
                     cool_down_seconds)
                 VALUES (?, ?, 30000, 120000, 1, \'workspace\', 5, 300)
                 ON CONFLICT (agent_name) DO NOTHING',
                [$name, $tier],
            );
        }
    }
}
