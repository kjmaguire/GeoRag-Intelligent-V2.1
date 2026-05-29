<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin\AgentConfig;

use App\Http\Controllers\Controller;
use App\Http\Requests\Admin\AgentConfig\UpdateWorkspaceConfigRequest;
use App\Services\Audit\AuditEmitter;
use Illuminate\Http\RedirectResponse;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Phase 0 Step 5.2 — `/admin/agent-config/workspaces`.
 *
 * Lists `workspace.workspace_agent_config` (per-workspace agent overrides).
 * Operators toggle enabled/disabled and edit the `config` JSONB blob.
 *
 * Each update writes the row + a `workspace.workspace_agent_config.update`
 * audit_ledger entry inside one DB::transaction(). The audit row carries
 * the workspace_id so RLS-scoped audit queries can find it later.
 */
class WorkspacesController extends Controller
{
    public function index(): Response
    {
        $this->authorize('admin');

        $rows = DB::connection('pgsql')
            ->table('workspace.workspace_agent_config')
            ->orderBy('workspace_id')
            ->orderBy('agent_name')
            ->get()
            ->map(function (object $row): array {
                $config = is_string($row->config) ? json_decode($row->config, true) : $row->config;

                return [
                    'id' => $row->id,
                    'workspace_id' => $row->workspace_id,
                    'agent_name' => $row->agent_name,
                    'config' => is_array($config) ? $config : [],
                    'enabled' => (bool) $row->enabled,
                    'updated_at' => $row->updated_at,
                    'updated_by' => $row->updated_by !== null ? (int) $row->updated_by : null,
                ];
            })
            ->all();

        return Inertia::render('Admin/AgentConfig/Workspaces', [
            'workspace_agent_configs' => $rows,
        ]);
    }

    public function update(UpdateWorkspaceConfigRequest $request, string $id): RedirectResponse
    {
        $this->authorize('admin');

        $data = $request->validated();
        $userId = (int) $request->user()->id;

        DB::transaction(function () use ($id, $data, $userId): void {
            $row = DB::connection('pgsql')
                ->table('workspace.workspace_agent_config')
                ->where('id', $id)
                ->first();

            abort_if($row === null, 404, "Unknown workspace_agent_config id: {$id}");

            $configJson = json_encode(
                $data['config'],
                JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE
            );

            DB::connection('pgsql')->statement(
                <<<'SQL'
                    UPDATE workspace.workspace_agent_config
                       SET enabled    = ?,
                           config     = ?::jsonb,
                           updated_at = now(),
                           updated_by = ?
                     WHERE id = ?::uuid
                SQL,
                [$data['enabled'], $configJson, $userId, $id],
            );

            app(AuditEmitter::class)->emit(
                actionType: 'workspace.workspace_agent_config.update',
                workspaceId: $row->workspace_id,
                actorId: $userId,
                actorKind: AuditEmitter::ACTOR_USER,
                targetSchema: 'workspace',
                targetTable: 'workspace_agent_config',
                targetId: $id,
                payload: [
                    'agent_name' => $row->agent_name,
                    'enabled' => $data['enabled'],
                    'config' => $data['config'],
                ],
            );
        });

        return redirect()
            ->route('admin.agent-config.workspaces')
            ->with('success', 'Workspace agent config updated.');
    }
}
