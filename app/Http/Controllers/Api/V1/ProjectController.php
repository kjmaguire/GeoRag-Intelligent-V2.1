<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Events\Workspace\WorkspaceActivityBroadcast;
use App\Http\Controllers\Controller;
use App\Http\Requests\StoreProjectRequest;
use App\Http\Requests\UpdateProjectRequest;
use App\Http\Resources\ProjectResource;
use App\Models\Project;
use App\Support\AuthorizationAuditLogger;
use Illuminate\Database\Eloquent\ModelNotFoundException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Http\Resources\Json\AnonymousResourceCollection;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Throwable;

class ProjectController extends Controller
{
    /**
     * List all projects, paginated.
     *
     * GET /api/v1/projects
     */
    public function index(Request $request): AnonymousResourceCollection
    {
        // Scope to the authenticated user's project memberships.
        $projectIds = $request->user()->projects()->pluck('silver.projects.project_id');

        $projects = Project::withCount('collars')
            ->whereIn('project_id', $projectIds)
            ->orderBy('created_at', 'desc')
            ->paginate($request->integer('per_page', 25));

        return ProjectResource::collection($projects);
    }

    /**
     * Create a new project.
     *
     * POST /api/v1/projects
     */
    public function store(StoreProjectRequest $request): JsonResponse
    {
        try {
            $project = new Project($request->validated());
            // Audit item J1 (2026-06-03) — resolve workspace via the
            // workspace_user pivot (User->defaultWorkspaceId()) introduced
            // in audit item A. The legacy users-table column form was
            // never authoritative; the pivot is. Hardcoded UUID stays only
            // as the very-last-ditch fallback for a fresh single-tenant
            // bootstrap where no pivot rows exist yet — this matches
            // OnboardingController's seeded default workspace.
            $user = $request->user();
            $project->workspace_id = $user->defaultWorkspaceId()
                ?? 'a0000000-0000-0000-0000-000000000001';
            $project->save();
            // Automatically add the creator as owner.
            $request->user()->projects()->attach($project->project_id, ['role' => 'owner']);
            $project->loadCount('collars');

            // Phase 3 — broadcast workspace activity so Foundry/Portfolio
            // and Foundry/Projects refetch the project list + KPIs.
            // Best-effort; broadcast failure must not fail project creation.
            $this->broadcastProjectMutation((string) $project->workspace_id, 'created', (string) $project->project_id);

            return (new ProjectResource($project))
                ->response()
                ->setStatusCode(201);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'message' => 'Failed to create project.',
                'error' => $e->getMessage(),
            ], 500);
        }
    }

    /**
     * Show a single project with its collar count.
     *
     * GET /api/v1/projects/{project}
     *
     * Returns 404 (not 403) when the user lacks membership so we do not
     * leak whether the UUID exists to a potential attacker (existence oracle
     * defence). The access check fires BEFORE findOrFail for the same reason.
     */
    public function show(Request $request, string $projectId): JsonResponse
    {
        // Gate: membership check before the DB lookup to prevent timing
        // differences or message differences revealing UUID existence.
        if (! $request->user()->hasProjectAccess($projectId)) {
            // Module 9 Chunk 9.8 — structured authz audit event.
            AuthorizationAuditLogger::deny(
                actor: $request->user(),
                targetResource: "project:{$projectId}",
                reason: 'no_pivot_row',
                context: ['action' => 'show', 'path' => $request->path()],
            );

            return response()->json(['message' => 'Project not found.'], 404);
        }

        try {
            $project = Project::withCount('collars')
                ->findOrFail($projectId);

            return (new ProjectResource($project))->response();
        } catch (ModelNotFoundException) {
            return response()->json(['message' => 'Project not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'message' => 'Failed to retrieve project.',
                'error' => $e->getMessage(),
            ], 500);
        }
    }

    /**
     * Update an existing project.
     *
     * PUT/PATCH /api/v1/projects/{project}
     *
     * Returns 404 (not 403) when the user lacks membership — existence oracle
     * defence. The membership check fires BEFORE findOrFail.
     */
    public function update(UpdateProjectRequest $request, string $projectId): JsonResponse
    {
        // Gate: membership check before the DB lookup.
        if (! $request->user()->hasProjectAccess($projectId)) {
            AuthorizationAuditLogger::deny(
                actor: $request->user(),
                targetResource: "project:{$projectId}",
                reason: 'no_pivot_row',
                context: ['action' => 'update', 'path' => $request->path()],
            );

            return response()->json(['message' => 'Project not found.'], 404);
        }

        try {
            $project = Project::findOrFail($projectId);
            $project->update($request->validated());
            $project->loadCount('collars');

            // Phase 3 — broadcast workspace activity. Project rename / region
            // / commodity edits change the Portfolio + Projects list rendering.
            $this->broadcastProjectMutation((string) $project->workspace_id, 'updated', (string) $project->project_id);

            return (new ProjectResource($project))->response();
        } catch (ModelNotFoundException) {
            return response()->json(['message' => 'Project not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'message' => 'Failed to update project.',
                'error' => $e->getMessage(),
            ], 500);
        }
    }

    /**
     * Delete a project (cascades to collars and all child records).
     *
     * DELETE /api/v1/projects/{project}
     *
     * Returns 404 (not 403) when the user lacks membership — existence oracle
     * defence. The membership check fires BEFORE findOrFail.
     */
    public function destroy(Request $request, string $projectId): JsonResponse
    {
        // Gate: membership check before the DB lookup.
        if (! $request->user()->hasProjectAccess($projectId)) {
            AuthorizationAuditLogger::deny(
                actor: $request->user(),
                targetResource: "project:{$projectId}",
                reason: 'no_pivot_row',
                context: ['action' => 'destroy', 'path' => $request->path()],
            );

            return response()->json(['message' => 'Project not found.'], 404);
        }

        try {
            $project = Project::findOrFail($projectId);
            $workspaceId = (string) $project->workspace_id;

            // Cascade FKs handle most child rows (collars, drill_traces, project_user,
            // geochemistry, exports, project_boundaries, geological_formations,
            // historic_workings, target_candidate_zones, saved_map_views,
            // collaboration_*). The remaining tables either SET NULL (orphans the row)
            // or RESTRICT (blocks the delete). Per user choice "wipe everything for
            // this project", explicitly delete those before dropping the project row.
            DB::transaction(function () use ($project, $projectId) {
                $tables = [
                    // SET NULL relations — would orphan otherwise
                    'silver.reports',
                    'silver.spatial_features',
                    'silver.seismic_surveys',
                    'silver.raster_layers',
                    'silver.answer_runs',
                    'silver.geophysics_surveys',
                    // RESTRICT / NO ACTION relations — would block the delete
                    'silver.mineral_claims',
                    'silver.review_queue',
                    'silver.campaigns',
                    'gold.zone_statistics',
                    'gold.element_correlations',
                ];
                foreach ($tables as $table) {
                    DB::table($table)
                        ->where('project_id', $projectId)
                        ->delete();
                }

                $project->delete();
            });

            // Phase 3 — broadcast workspace activity so Portfolio + Projects
            // drop the row from their lists. Fires AFTER the delete commits so
            // a re-fetch sees the row already gone. Best-effort.
            $this->broadcastProjectMutation($workspaceId, 'deleted', $projectId);

            return response()->json(null, 204);
        } catch (ModelNotFoundException) {
            return response()->json(['message' => 'Project not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'message' => 'Failed to delete project.',
                'error' => $e->getMessage(),
            ], 500);
        }
    }

    /**
     * Fire a WorkspaceActivityBroadcast for project mutations.
     *
     * Best-effort — wrapped in try/catch so a broadcasting outage cannot
     * roll back the controller action that just committed. The durable
     * record is the project row; this is the latency optimisation that
     * lets Portfolio + Projects re-fetch without manual reload.
     *
     * @param 'created'|'updated'|'deleted' $verb
     */
    private function broadcastProjectMutation(string $workspaceId, string $verb, string $projectId): void
    {
        try {
            WorkspaceActivityBroadcast::dispatch(
                $workspaceId,
                ['projects', 'kpis'],
                [
                    'verb' => $verb,
                    'project_id' => $projectId,
                ],
            );
        } catch (Throwable $e) {
            Log::warning('ProjectController: workspace.activity broadcast failed', [
                'workspace_id' => $workspaceId,
                'verb' => $verb,
                'project_id' => $projectId,
                'error' => $e->getMessage(),
            ]);
        }
    }
}
