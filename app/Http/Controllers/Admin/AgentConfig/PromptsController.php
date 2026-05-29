<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin\AgentConfig;

use App\Http\Controllers\Controller;
use App\Http\Requests\Admin\AgentConfig\PromotePromptRequest;
use App\Services\Audit\AuditEmitter;
use Illuminate\Http\RedirectResponse;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Phase 0 Step 5.2 — `/admin/agent-config/prompts`.
 *
 * Lists `workspace.prompt_versions` grouped by `prompt_id`, surfacing the
 * promotion lifecycle: draft → staging → production → deprecated.
 *
 * Phase 0 ships this surface unguarded admin-only. The Prompt Release
 * Approval Agent (Phase 4) will eventually gate the staging→production
 * transition; until then the admin promotes by hand.
 *
 * Promoting a version to `production` automatically deprecates the
 * existing production version for that prompt_id (the partial unique
 * index `prompt_versions_one_production_per_prompt` enforces at most
 * one). Both row UPDATEs and the audit emit happen inside one
 * DB::transaction(). action_type: `workspace.prompt_versions.promote`.
 */
class PromptsController extends Controller
{
    public function index(): Response
    {
        $this->authorize('admin');

        $rows = DB::connection('pgsql')
            ->table('workspace.prompt_versions')
            ->orderBy('prompt_id')
            ->orderByDesc('created_at')
            ->get()
            ->map(fn (object $row): array => [
                'id' => $row->id,
                'prompt_id' => $row->prompt_id,
                'version' => $row->version,
                'promotion_state' => $row->promotion_state,
                'promoted_at' => $row->promoted_at,
                'deprecated_at' => $row->deprecated_at,
                'created_at' => $row->created_at,
                'created_by' => $row->created_by !== null ? (int) $row->created_by : null,
                'notes' => $row->notes,
            ])
            ->all();

        // Group by prompt_id for the UI's per-prompt history block.
        $grouped = [];
        foreach ($rows as $row) {
            $grouped[$row['prompt_id']][] = $row;
        }
        ksort($grouped);

        return Inertia::render('Admin/AgentConfig/Prompts', [
            'prompts' => array_map(
                fn (string $promptId, array $versions): array => [
                    'prompt_id' => $promptId,
                    'versions' => $versions,
                ],
                array_keys($grouped),
                array_values($grouped),
            ),
        ]);
    }

    public function promote(PromotePromptRequest $request, string $id): RedirectResponse
    {
        $this->authorize('admin');

        $data = $request->validated();
        $userId = (int) $request->user()->id;
        $newState = $data['promotion_state'];

        DB::transaction(function () use ($id, $newState, $userId): void {
            $row = DB::connection('pgsql')
                ->table('workspace.prompt_versions')
                ->where('id', $id)
                ->first();

            abort_if($row === null, 404, "Unknown prompt_version id: {$id}");

            $previousState = $row->promotion_state;

            // Demote any current production for this prompt_id before
            // promoting a different row to production. This sidesteps
            // the partial unique index violation rather than relying on
            // RAISE inside a trigger.
            if ($newState === 'production' && $previousState !== 'production') {
                DB::connection('pgsql')
                    ->table('workspace.prompt_versions')
                    ->where('prompt_id', $row->prompt_id)
                    ->where('promotion_state', 'production')
                    ->where('id', '!=', $id)
                    ->update([
                        'promotion_state' => 'deprecated',
                        'deprecated_at' => now(),
                    ]);
            }

            $update = ['promotion_state' => $newState];
            if ($newState === 'production') {
                $update['promoted_at'] = now();
            } elseif ($newState === 'deprecated') {
                $update['deprecated_at'] = now();
            }

            DB::connection('pgsql')
                ->table('workspace.prompt_versions')
                ->where('id', $id)
                ->update($update);

            app(AuditEmitter::class)->emit(
                actionType: 'workspace.prompt_versions.promote',
                actorId: $userId,
                actorKind: AuditEmitter::ACTOR_USER,
                targetSchema: 'workspace',
                targetTable: 'prompt_versions',
                targetId: $id,
                payload: [
                    'prompt_id' => $row->prompt_id,
                    'version' => $row->version,
                    'previous_state' => $previousState,
                    'new_state' => $newState,
                ],
            );
        });

        return redirect()
            ->route('admin.agent-config.prompts')
            ->with('success', "Promoted prompt version to {$newState}.");
    }
}
