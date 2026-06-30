<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin\AgentConfig;

use App\Http\Controllers\Controller;
use App\Http\Requests\Admin\AgentConfig\UpdateTimeoutRequest;
use App\Services\Audit\AuditEmitter;
use Illuminate\Http\RedirectResponse;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Phase 0 Step 5.2 — `/admin/agent-config/timeouts`.
 *
 * Lists rows of `workspace.agent_timeouts` (per-agent timeout + retry +
 * circuit-breaker policy). Operators tune soft/hard timeouts, retry count,
 * circuit-breaker scope, failure threshold, and cool-down per agent_name.
 *
 * Every UPDATE is wrapped with the state-changing write inside a
 * `DB::transaction()` together with an `AuditEmitter::emit()` so the
 * audit row commits atomically with the data change. action_type:
 * `workspace.agent_timeouts.update`.
 *
 * Auth: 'admin' Gate (users.is_admin = true), defined in
 * AppServiceProvider::boot(). Same pattern as WorkflowRunController.
 */
class TimeoutsController extends Controller
{
    public function index(): Response
    {
        $this->authorize('admin');

        $rows = DB::connection('pgsql')
            ->table('workspace.agent_timeouts')
            ->orderBy('agent_name')
            ->get()
            ->map(fn (object $row): array => [
                'agent_name' => $row->agent_name,
                'risk_tier' => $row->risk_tier,
                'soft_timeout_ms' => (int) $row->soft_timeout_ms,
                'hard_timeout_ms' => (int) $row->hard_timeout_ms,
                'retry_count' => (int) $row->retry_count,
                'circuit_breaker_scope' => $row->circuit_breaker_scope,
                'failure_threshold' => (int) $row->failure_threshold,
                'cool_down_seconds' => (int) $row->cool_down_seconds,
                'updated_at' => $row->updated_at,
                'updated_by' => $row->updated_by !== null ? (int) $row->updated_by : null,
            ])
            ->all();

        return Inertia::render('Admin/AgentConfig/Timeouts', [
            'timeouts' => $rows,
        ]);
    }

    public function update(UpdateTimeoutRequest $request, string $agentName): RedirectResponse
    {
        $this->authorize('admin');

        $data = $request->validated();
        $userId = (int) $request->user()->id;

        DB::transaction(function () use ($agentName, $data, $userId): void {
            $affected = DB::connection('pgsql')
                ->table('workspace.agent_timeouts')
                ->where('agent_name', $agentName)
                ->update([
                    'soft_timeout_ms' => $data['soft_timeout_ms'],
                    'hard_timeout_ms' => $data['hard_timeout_ms'],
                    'retry_count' => $data['retry_count'],
                    'circuit_breaker_scope' => $data['circuit_breaker_scope'],
                    'failure_threshold' => $data['failure_threshold'],
                    'cool_down_seconds' => $data['cool_down_seconds'],
                    'updated_at' => now(),
                    'updated_by' => $userId,
                ]);

            abort_if($affected === 0, 404, "Unknown agent_name: {$agentName}");

            app(AuditEmitter::class)->emit(
                actionType: 'workspace.agent_timeouts.update',
                actorId: $userId,
                actorKind: AuditEmitter::ACTOR_USER,
                targetSchema: 'workspace',
                targetTable: 'agent_timeouts',
                targetId: $agentName,
                payload: [
                    'soft_timeout_ms' => $data['soft_timeout_ms'],
                    'hard_timeout_ms' => $data['hard_timeout_ms'],
                    'retry_count' => $data['retry_count'],
                    'circuit_breaker_scope' => $data['circuit_breaker_scope'],
                    'failure_threshold' => $data['failure_threshold'],
                    'cool_down_seconds' => $data['cool_down_seconds'],
                ],
            );
        });

        return redirect()
            ->route('admin.agent-config.timeouts')
            ->with('success', "Updated timeouts for {$agentName}.");
    }
}
