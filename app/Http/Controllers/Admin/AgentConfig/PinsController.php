<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin\AgentConfig;

use App\Http\Controllers\Controller;
use App\Http\Requests\Admin\AgentConfig\UpdatePinRequest;
use App\Services\Audit\AuditEmitter;
use Illuminate\Http\RedirectResponse;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Phase 0 Step 5.2 — `/admin/agent-config/pins`.
 *
 * Lists `workspace.agent_prompt_pins` (per-agent prompt-version pin).
 * NULL prompt_version_id means "fall through to the production-promoted
 * row for this prompt_id"; a non-null pin overrides that.
 *
 * Each update writes the row + an `workspace.agent_prompt_pins.update`
 * audit_ledger entry inside one DB::transaction().
 */
class PinsController extends Controller
{
    public function index(): Response
    {
        $this->authorize('admin');

        $pins = DB::connection('pgsql')
            ->table('workspace.agent_prompt_pins as p')
            ->leftJoin(
                'workspace.prompt_versions as v',
                'p.prompt_version_id',
                '=',
                'v.id',
            )
            ->orderBy('p.agent_name')
            ->select([
                'p.agent_name',
                'p.prompt_id',
                'p.prompt_version_id',
                'p.pinned_at',
                'p.pinned_by',
                'p.updated_at',
                'v.version as pinned_version_label',
                'v.promotion_state as pinned_promotion_state',
            ])
            ->get()
            ->map(fn (object $row): array => [
                'agent_name' => $row->agent_name,
                'prompt_id' => $row->prompt_id,
                'prompt_version_id' => $row->prompt_version_id,
                'pinned_version_label' => $row->pinned_version_label,
                'pinned_promotion_state' => $row->pinned_promotion_state,
                'pinned_at' => $row->pinned_at,
                'pinned_by' => $row->pinned_by !== null ? (int) $row->pinned_by : null,
                'updated_at' => $row->updated_at,
            ])
            ->all();

        // Group available versions by prompt_id so the UI can populate a
        // dropdown of valid candidates per row without an extra request.
        $availableVersions = DB::connection('pgsql')
            ->table('workspace.prompt_versions')
            ->orderBy('prompt_id')
            ->orderByDesc('created_at')
            ->get()
            ->groupBy('prompt_id')
            ->map(fn ($rows) => $rows->map(fn (object $r): array => [
                'id' => $r->id,
                'version' => $r->version,
                'promotion_state' => $r->promotion_state,
            ])->values()->all())
            ->all();

        return Inertia::render('Admin/AgentConfig/Pins', [
            'pins' => $pins,
            'available_versions' => $availableVersions,
        ]);
    }

    public function update(UpdatePinRequest $request, string $agentName): RedirectResponse
    {
        $this->authorize('admin');

        $data = $request->validated();
        $userId = (int) $request->user()->id;
        $newVersionId = $data['prompt_version_id'] ?? null;

        DB::transaction(function () use ($agentName, $newVersionId, $userId): void {
            $pin = DB::connection('pgsql')
                ->table('workspace.agent_prompt_pins')
                ->where('agent_name', $agentName)
                ->first();

            abort_if($pin === null, 404, "No pin row for agent: {$agentName}");

            if ($newVersionId !== null) {
                $version = DB::connection('pgsql')
                    ->table('workspace.prompt_versions')
                    ->where('id', $newVersionId)
                    ->first(['id', 'prompt_id']);

                abort_if($version === null, 422, 'Unknown prompt_version_id.');
                abort_if(
                    $version->prompt_id !== $pin->prompt_id,
                    422,
                    "prompt_version_id belongs to prompt_id={$version->prompt_id}, "
                        ."but the pin is for prompt_id={$pin->prompt_id}.",
                );
            }

            DB::connection('pgsql')
                ->table('workspace.agent_prompt_pins')
                ->where('agent_name', $agentName)
                ->update([
                    'prompt_version_id' => $newVersionId,
                    'pinned_at' => $newVersionId !== null ? now() : null,
                    'pinned_by' => $newVersionId !== null ? $userId : null,
                    'updated_at' => now(),
                ]);

            app(AuditEmitter::class)->emit(
                actionType: 'workspace.agent_prompt_pins.update',
                actorId: $userId,
                actorKind: AuditEmitter::ACTOR_USER,
                targetSchema: 'workspace',
                targetTable: 'agent_prompt_pins',
                targetId: $agentName,
                payload: [
                    'prompt_id' => $pin->prompt_id,
                    'previous_prompt_version_id' => $pin->prompt_version_id,
                    'new_prompt_version_id' => $newVersionId,
                ],
            );
        });

        return redirect()
            ->route('admin.agent-config.pins')
            ->with('success', "Updated prompt pin for {$agentName}.");
    }
}
