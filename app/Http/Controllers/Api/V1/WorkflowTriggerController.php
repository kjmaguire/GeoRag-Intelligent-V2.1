<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Models\User;
use App\Services\HatchetWorkflowTrigger;
use App\Services\HatchetWorkflowTriggerException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Carbon;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Gate;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;
use Illuminate\Validation\Rule;
use Illuminate\Validation\ValidationException;

/**
 * Product triggers for the Hatchet workflows that were reachable only from
 * the Hatchet UI (HAT-13, 2026-09-29).
 *
 *   POST /api/v1/projects/{project}/workflows/{workflow}
 *        generate_report, score_targets        project members
 *   POST /api/v1/admin/workspaces/{workspace}/workflows/{workflow}
 *        workspace_export, restore_workspace, lineage_walk,
 *        support_packet_assemble, support_replay
 *                                              admins who belong to the workspace
 *   POST /api/v1/admin/workflows/{workflow}
 *        llm_incident_diagnosis_run            admins
 *
 * Every route answers 202 with `{workflow, workflow_run_id, workspace_id}`
 * and does not wait for the run. The decision about the user belongs to
 * WorkflowTriggerPolicy. FastAPI (`/internal/v1/workflows/{workflow}/trigger`)
 * validates the input against the workflow's own model and refuses anything
 * that names a row outside the authorised workspace.
 *
 * The workflow input is built here, from the authenticated user. Identity
 * fields (`requested_by_user_id`, `initiated_by_user_id`, `actor_id`) and
 * idempotency keys are never taken from the request body.
 *
 * Double-click guard: an identical request (same workflow, scope and body)
 * within COOLDOWN_SECONDS answers 429 with the first run's id instead of
 * enqueueing a second run. A different body is a different request.
 *
 * Nothing here is a Horizon job. These are Hatchet workflows, and Laravel
 * only hands them over (CLAUDE.md rule 7).
 */
final class WorkflowTriggerController extends Controller
{
    public const COOLDOWN_SECONDS = 60;

    /**
     * report_type values the §15.1 Report Builder accepts
     * (app/services/report_builder/state.py ReportType).
     */
    public const REPORT_TYPES = [
        'weekly_project_digest',
        'ingestion_quality',
        'technical_due_diligence',
        'executive_project_intelligence',
        'gis_arcgis_sync',
        'target_recommendation',
        'public_geo_overlay',
        'data_room_package',
        'what_changed',
        'ni43101_section_pack',
        'csa11348_disclosure_pack',
    ];

    private const EXPORT_BUCKET = 'workspace-exports';

    public function project(
        Request $request,
        string $project,
        string $workflow,
        HatchetWorkflowTrigger $trigger,
    ): JsonResponse {
        $user = $this->user($request);

        /** @var Project|null $model */
        $model = Project::query()->find($project);
        if ($model === null || Gate::forUser($user)->denies('triggerProjectWorkflow', [$model, $workflow])) {
            // 404, not 403: a non-member must not learn the project exists.
            return response()->json(['message' => 'Project not found.'], 404);
        }

        $workspaceId = $model->workspace_id !== null ? (string) $model->workspace_id : '';
        if ($workspaceId === '') {
            return response()->json([
                'error' => 'project_has_no_workspace',
                'message' => 'This project is not attached to a workspace.',
            ], 409);
        }

        $projectId = (string) $model->project_id;
        [$fingerprint, $input] = match ($workflow) {
            'generate_report' => $this->generateReportInput($request, $user, $workspaceId, $projectId),
            'score_targets' => $this->scoreTargetsInput($request, $user, $workspaceId, $projectId),
            default => abort(404),
        };

        return $this->dispatch($trigger, $workflow, $workspaceId, $input, $user, $projectId, $fingerprint);
    }

    public function workspace(
        Request $request,
        string $workspace,
        string $workflow,
        HatchetWorkflowTrigger $trigger,
    ): JsonResponse {
        $user = $this->user($request);
        $this->authorize('admin');

        $workspace = strtolower($workspace);
        if (Gate::forUser($user)->denies('triggerWorkspaceWorkflow', [$workspace, $workflow])) {
            return response()->json(['message' => 'Workspace not found.'], 404);
        }

        [$fingerprint, $input] = match ($workflow) {
            'workspace_export' => [[], ['workspace_id' => $workspace]],
            'restore_workspace' => $this->restoreInput($request, $user, $workspace),
            'lineage_walk' => $this->lineageInput($request, $user, $workspace),
            'support_packet_assemble' => $this->supportPacketInput($request, $user, $workspace),
            'support_replay' => $this->supportReplayInput($request, $user),
            default => abort(404),
        };

        return $this->dispatch($trigger, $workflow, $workspace, $input, $user, 'workspace:'.$workspace, $fingerprint);
    }

    public function platform(Request $request, string $workflow, HatchetWorkflowTrigger $trigger): JsonResponse
    {
        $user = $this->user($request);
        $this->authorize('admin');
        abort_if(Gate::forUser($user)->denies('triggerPlatformWorkflow', [$workflow]), 404);

        $validated = $request->validate([
            'alert_label' => ['required', 'string', 'max:200'],
            'window_minutes' => ['nullable', 'integer', 'min:1', 'max:1440'],
        ]);
        $kwargs = array_filter([
            'alert_label' => $validated['alert_label'],
            'window_minutes' => isset($validated['window_minutes']) ? (int) $validated['window_minutes'] : null,
        ], static fn (mixed $v): bool => $v !== null);

        return $this->dispatch(
            $trigger,
            $workflow,
            null,
            ['workspace_id' => null, 'actor_id' => $user->getKey(), 'kwargs' => $kwargs],
            $user,
            'platform',
            $kwargs,
        );
    }

    // ── Per-workflow input ──────────────────────────────────────────────

    /**
     * @return array{0: array<string, mixed>, 1: array<string, mixed>}
     */
    private function generateReportInput(Request $request, User $user, string $workspaceId, string $projectId): array
    {
        $validated = $request->validate([
            'report_type' => ['required', 'string', Rule::in(self::REPORT_TYPES)],
            'report_window_start' => ['nullable', 'date'],
            'report_window_end' => array_filter([
                'nullable',
                'date',
                $request->filled('report_window_start') ? 'after_or_equal:report_window_start' : null,
            ]),
        ]);

        $iso = static fn (mixed $value): ?string => is_string($value) && $value !== ''
            ? Carbon::parse($value)->utc()->toIso8601String()
            : null;

        $fingerprint = [
            'report_type' => $validated['report_type'],
            'report_window_start_iso' => $iso($validated['report_window_start'] ?? null),
            'report_window_end_iso' => $iso($validated['report_window_end'] ?? null),
        ];

        return [$fingerprint, [
            'workspace_id' => $workspaceId,
            'project_id' => $projectId,
            'requested_by_user_id' => $user->getKey(),
            'export_request_id' => (string) Str::uuid(),
            ...$fingerprint,
        ]];
    }

    /**
     * @return array{0: array<string, mixed>, 1: array<string, mixed>}
     */
    private function scoreTargetsInput(Request $request, User $user, string $workspaceId, string $projectId): array
    {
        $validated = $request->validate([
            'aoi_geom_wkt' => ['required', 'string', 'max:200000'],
            // Required until the generate_candidate_zones node graduates: with
            // no zones the graph has nothing to score.
            'candidate_zone_wkts' => ['required', 'array', 'min:1', 'max:200'],
            'candidate_zone_wkts.*' => ['required', 'string', 'max:200000'],
            'target_model_slug' => ['nullable', 'string', 'max:100', 'regex:/^[a-z0-9_\-]+$/'],
            'target_commodity' => ['nullable', 'string', 'max:20'],
            'scoring_kind' => ['nullable', 'string', Rule::in(['weighted', 'xgboost', 'ensemble'])],
        ]);

        $fingerprint = [
            'aoi_geom_wkt' => $validated['aoi_geom_wkt'],
            'extra_candidate_zone_wkts' => array_values($validated['candidate_zone_wkts']),
            'target_model_slug' => $validated['target_model_slug'] ?? null,
            'target_commodity' => $validated['target_commodity'] ?? null,
            'scoring_kind' => $validated['scoring_kind'] ?? 'weighted',
        ];

        return [$fingerprint, [
            'workspace_id' => $workspaceId,
            'project_id' => $projectId,
            'requested_by_user_id' => $user->getKey(),
            'score_request_id' => (string) Str::uuid(),
            ...$fingerprint,
        ]];
    }

    /**
     * @return array{0: array<string, mixed>, 1: array<string, mixed>}
     */
    private function restoreInput(Request $request, User $user, string $workspace): array
    {
        $prefix = 's3://'.self::EXPORT_BUCKET.'/'.$workspace.'/';
        $validated = $request->validate([
            'snapshot_manifest_uri' => ['required', 'string', 'max:1024', 'starts_with:'.$prefix, 'not_regex:/\.\./'],
            'dry_run' => ['sometimes', 'boolean'],
            'confirm_workspace_id' => ['nullable', 'string'],
        ]);

        $dryRun = $request->boolean('dry_run', true);
        if (! $dryRun && strtolower((string) ($validated['confirm_workspace_id'] ?? '')) !== $workspace) {
            // A real restore writes into the workspace. Make the operator type
            // its id, so a stray dry_run=false cannot start one.
            throw ValidationException::withMessages([
                'confirm_workspace_id' => 'A restore with dry_run=false needs confirm_workspace_id set to this workspace id.',
            ]);
        }

        $fingerprint = ['snapshot_manifest_uri' => $validated['snapshot_manifest_uri'], 'dry_run' => $dryRun];

        return [$fingerprint, [
            'workspace_id' => $workspace,
            'initiated_by_user_id' => $user->getKey(),
            'restore_request_id' => (string) Str::uuid(),
            ...$fingerprint,
        ]];
    }

    /**
     * @return array{0: array<string, mixed>, 1: array<string, mixed>}
     */
    private function lineageInput(Request $request, User $user, string $workspace): array
    {
        $validated = $request->validate([
            'target_type' => ['required', 'string', Rule::in(['workflow_run', 'audit_ledger_entry', 'workspace'])],
            'target_id' => ['exclude_if:target_type,workspace', 'required', 'string', 'max:200'],
            'limit' => ['nullable', 'integer', 'min:1', 'max:1000'],
        ]);

        $kwargs = [
            'target_type' => $validated['target_type'],
            'target_id' => $validated['target_type'] === 'workspace' ? $workspace : $validated['target_id'],
            'limit' => isset($validated['limit']) ? (int) $validated['limit'] : 1000,
        ];

        return [$kwargs, ['workspace_id' => $workspace, 'actor_id' => $user->getKey(), 'kwargs' => $kwargs]];
    }

    /**
     * @return array{0: array<string, mixed>, 1: array<string, mixed>}
     */
    private function supportPacketInput(Request $request, User $user, string $workspace): array
    {
        $validated = $request->validate([
            'incident_id' => ['required', 'string', 'max:200'],
            'trace_id' => ['nullable', 'string', 'max:200'],
        ]);

        $fingerprint = array_filter([
            'incident_id' => $validated['incident_id'],
            'trace_id' => $validated['trace_id'] ?? null,
        ], static fn (mixed $v): bool => $v !== null);

        return [$fingerprint, [
            'workspace_id' => $workspace,
            'actor_id' => $user->getKey(),
            'trace_id' => $fingerprint['trace_id'] ?? null,
            'kwargs' => [...$fingerprint, 'requested_by' => $user->getKey()],
        ]];
    }

    /**
     * @return array{0: array<string, mixed>, 1: array<string, mixed>}
     */
    private function supportReplayInput(Request $request, User $user): array
    {
        $validated = $request->validate([
            'ticket_id' => ['required', 'uuid'],
            'original_workflow_run_id' => ['required', 'string', 'max:200'],
        ]);

        $fingerprint = [
            'ticket_id' => strtolower($validated['ticket_id']),
            'original_workflow_run_id' => $validated['original_workflow_run_id'],
        ];

        return [$fingerprint, [
            ...$fingerprint,
            'initiated_by_user_id' => $user->getKey(),
            // Always a dry run: a live replay needs operator AND
            // workspace-owner consent, and no consent flow exists.
            'dry_run' => true,
            'replay_request_id' => (string) Str::uuid(),
        ]];
    }

    // ── Shared ──────────────────────────────────────────────────────────

    private function user(Request $request): User
    {
        $user = $request->user();
        abort_unless($user instanceof User, 401);

        return $user;
    }

    /**
     * @param array<string, mixed> $input
     * @param array<string, mixed> $fingerprint the caller-chosen part of the request
     */
    private function dispatch(
        HatchetWorkflowTrigger $trigger,
        string $workflow,
        ?string $workspaceId,
        array $input,
        User $user,
        string $jwtProject,
        array $fingerprint,
    ): JsonResponse {
        $cooldownKey = 'workflow-trigger:'.$workflow.':'.sha1(
            $user->getKey().'|'.$jwtProject.'|'.json_encode($fingerprint, JSON_THROW_ON_ERROR),
        );
        $claim = ['workflow_run_id' => null, 'triggered_at' => now()->toIso8601String()];

        if (! Cache::add($cooldownKey, $claim, self::COOLDOWN_SECONDS)) {
            /** @var array{workflow_run_id: ?string, triggered_at: string}|null $previous */
            $previous = Cache::get($cooldownKey);

            return response()->json([
                'error' => 'workflow_recently_triggered',
                'message' => 'The same '.$workflow.' request was sent less than '
                    .self::COOLDOWN_SECONDS.' seconds ago.',
                'workflow_run_id' => $previous['workflow_run_id'] ?? null,
                'triggered_at' => $previous['triggered_at'] ?? null,
            ], 429);
        }

        // The cooldown claim above exists to stop a double click dispatching
        // twice. It must therefore be released on EVERY path where nothing was
        // dispatched, not only on HatchetWorkflowTriggerException: any other
        // throwable (a JWT-mint failure, a serialisation error, a cache or
        // driver exception inside trigger()) used to leave the key set, so the
        // user's retry was answered 429 "sent less than 60 seconds ago" for a
        // request that never went anywhere.
        $dispatched = false;
        try {
            $result = $trigger->trigger($workflow, $workspaceId, $input, $user, $jwtProject);
            $dispatched = true;
        } catch (HatchetWorkflowTriggerException $exc) {
            // 5xx means FastAPI was unreachable or answered with an error, and
            // the message carries the upstream URL, driver text or response
            // body. That detail is for the log; the caller gets a neutral
            // line. 404/422 are FastAPI's own, user-actionable answers.
            if ($exc->status >= 500) {
                Log::warning('workflow_trigger.failed', [
                    'workflow' => $workflow,
                    'status' => $exc->status,
                    'detail' => $exc->getMessage(),
                ]);

                return response()->json([
                    'error' => 'trigger_failed',
                    'message' => 'The '.$workflow.' workflow could not be started right now. Please try again shortly.',
                ], $exc->status);
            }

            return response()->json(['error' => 'trigger_failed', 'message' => $exc->getMessage()], $exc->status);
        } finally {
            if (! $dispatched) {
                Cache::forget($cooldownKey);
            }
        }

        Cache::put($cooldownKey, [...$claim, 'workflow_run_id' => $result['workflow_run_id']], self::COOLDOWN_SECONDS);

        Log::info('workflow_trigger.dispatched', [
            'workflow' => $workflow,
            'workflow_run_id' => $result['workflow_run_id'],
            'workspace_id' => $workspaceId,
            'user_id' => $user->getKey(),
        ]);

        return response()->json($result, 202);
    }
}
