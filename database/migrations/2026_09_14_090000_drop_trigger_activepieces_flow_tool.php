<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Delete the `trigger_activepieces_flow` row from the agent tool registry.
 *
 * Background — Activepieces was the Phase 2 integration orchestrator, sunset
 * wholesale at Phase 3 Step 7. Its Kestra replacement was retired on
 * 2026-07-28 without ever having been deployed. The §4.2 tool list was
 * ported verbatim into `2026_08_28_100200_create_tool_gateway_tables`, which
 * carried this row forward on the stated reasoning that dropping a registry
 * row without dropping its impl turns a stub response into a hard rejection.
 *
 * That reasoning holds for `query_neo4j_readonly`, which
 * `services/tool_gateway/impls.py::register_all_impls` still registers as an
 * explicit no-op stub. It does NOT hold here: `trigger_activepieces_flow` has
 * no impl and never had one, so `invoke_tool()` already fails on it either
 * way — with the row, at impl lookup; without it, at
 * `policies.py::effective_tier` as an unknown tool. Removing the row changes
 * which rejection you get, not whether you get one.
 *
 * What it does change is the registry's honesty: an R3 tier row with a
 * `requires_dry_run` flag reads as a governed, available capability. It is
 * neither. The AWS migration (ADR-0022) is the moment to stop shipping it,
 * because every store starts empty and no invocation history depends on it.
 *
 * The seed in `2026_08_28_100200` is deliberately left as it was — editing an
 * applied migration does not change a cluster that has already run it, and
 * the honest record is that the row was seeded and then retired. On a fresh
 * database the seed inserts it and this migration removes it a few steps
 * later. `database/raw/phase0/108-section4-tool-gateway-schema.sql` no longer
 * seeds it at all.
 *
 * SQLite (test DB) has no `workspace` schema, so this is gated on Postgres.
 */
return new class extends Migration
{
    private const TOOL = 'trigger_activepieces_flow';

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // No FK references agent_risk_tiers.tool_name — agent_permissions and
        // approval_requirements carry it as a plain column — so the companion
        // rows have to be cleared explicitly or they outlive the registry
        // entry as unreachable per-workspace grants.
        DB::statement('DELETE FROM workspace.agent_permissions WHERE tool_name = ?', [self::TOOL]);
        DB::statement('DELETE FROM workspace.approval_requirements WHERE tool_name = ?', [self::TOOL]);
        DB::statement('DELETE FROM workspace.agent_risk_tiers WHERE tool_name = ?', [self::TOOL]);
    }

    public function down(): void
    {
        // No-op. Re-seeding the row would restore a tier for a tool with no
        // impl, which is the state this migration exists to end. The
        // `workspace.tool_invocations` audit ring is untouched either way —
        // it is keyed on tool_name as free text, so any historical call
        // remains queryable.
    }
};
