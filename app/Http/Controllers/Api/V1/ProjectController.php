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
use App\Support\PaginationLimit;
use App\Support\SafeErrorMessage;
use Illuminate\Database\Eloquent\ModelNotFoundException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Http\Resources\Json\AnonymousResourceCollection;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Schema;
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
            ->paginate(PaginationLimit::clamp($request, 25));

        return ProjectResource::collection($projects);
    }

    /**
     * Create a new project.
     *
     * POST /api/v1/projects
     */
    public function store(StoreProjectRequest $request): JsonResponse
    {
        $workspaceId = $this->resolveWorkspaceId($request);

        if ($workspaceId === null) {
            return response()->json([
                'message' => 'No workspace to create this project in. '
                    .'Ask an administrator to add you to a project first.',
            ], 422);
        }

        try {
            $project = new Project($request->validated());
            $project->workspace_id = $workspaceId;
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
                'error' => SafeErrorMessage::forResponse($e),
            ], 500);
        }
    }

    /**
     * Which tenant this project belongs in.
     *
     * Derived from the creator's existing project memberships, the same way
     * CitationController and PublicApiController derive tenancy. This used
     * to read `$request->user()->workspace_id` with a hardcoded fallback to
     * `a0000000-0000-0000-0000-000000000001` — and `users` has no
     * `workspace_id` column, so the fallback was not a fallback: EVERY
     * project created through this endpoint landed in that one workspace,
     * which is also where every migration and backfill puts real production
     * data. Combined with open registration, a stranger's first API call put
     * them inside the live tenant.
     *
     * An admin bootstrapping a fresh deployment has no memberships either,
     * so they may name the workspace explicitly; without one they get the
     * configured default. Everyone else must already belong somewhere.
     */
    private function resolveWorkspaceId(Request $request): ?string
    {
        $user = $request->user();

        if ($user === null) {
            return null;
        }

        $owned = $user->projects()
            ->pluck('silver.projects.workspace_id')
            ->filter()
            ->map(fn ($id): string => (string) $id)
            ->unique()
            ->values();

        $requested = $request->string('workspace_id')->trim()->value();

        if ($requested !== '') {
            // Naming a workspace you do not belong to is only an admin's
            // privilege; for everyone else it must be one of their own.
            if ($user->is_admin || $owned->contains($requested)) {
                return $requested;
            }

            return null;
        }

        if ($owned->count() === 1) {
            return $owned->first();
        }

        if ($owned->count() > 1) {
            // Ambiguous on purpose: pick one and half the user's projects end
            // up in the wrong tenant. Make them say which.
            return null;
        }

        return $user->is_admin
            ? (string) config('georag.default_workspace_id')
            : null;
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
                'error' => SafeErrorMessage::forResponse($e),
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
                'error' => SafeErrorMessage::forResponse($e),
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
            //
            // 2026-08-17 — this loop unconditionally deleted from every table in
            // the list, including silver.mineral_claims — which does not exist
            // in the live database at all (it was only ever created by an
            // out-of-band phase0 raw-SQL bootstrap script against the old
            // self-hosted Postgres, never a tracked migration; the canadacentral
            // Azure Postgres server provisioned during the Azure lift was built
            // from migrations + tracked bootstrap SQL only, so it never got that
            // table). `DELETE FROM silver.mineral_claims` therefore threw a
            // Postgres 42P01 "relation does not exist" error on EVERY delete
            // attempt, for every project, rolling back the whole transaction —
            // project deletion was 100% broken, not intermittent. SQLite-based
            // local tests never caught this because the test-DB parity
            // migration (2026_06_29_020000_provision_project_delete_tables_
            // for_test_db.php) always stubs a dummy mineral_claims table.
            // Skipping a listed table that doesn't exist in this environment
            // is now a no-op instead of a fatal error, so the same class of
            // drift (a table present in dev but never migrated to a fresh
            // environment) can't take project deletion down again.
            //
            // 2026-08-17 (second follow-up) — ORDER matters here and didn't
            // used to: silver.answer_citation_items has
            // `CHECK (evidence_id IS NOT NULL OR passage_id IS NOT NULL)`
            // (answer_citation_items_has_target) alongside two SET NULL FKs
            // — answer_run_id -> answer_runs (CASCADE) and
            // passage_id -> document_passages (SET NULL). Deleting
            // silver.reports first cascades to document_passages, which
            // fires the passage_id SET NULL on every citation item pointing
            // at those passages; a legacy citation row that only ever had
            // passage_id set (pre-evidence_id write path — see
            // 2026_04_21_150000_create_answer_citation_items's docblock)
            // then has BOTH target columns null and violates the CHECK,
            // aborting the whole transaction. Confirmed live on two
            // projects with real chat history. Deleting silver.answer_runs
            // FIRST cascades away its citation_items rows entirely before
            // reports/document_passages ever gets a chance to SET NULL
            // into them, so the CHECK is never evaluated against an
            // already-doomed row. answer_runs moved to the front of the
            // list for this reason — it must run before silver.reports.
            DB::transaction(function () use ($project, $projectId) {
                $tables = [
                    // Must run before silver.reports — see docblock above.
                    'silver.answer_runs',
                    // SET NULL relations — would orphan otherwise
                    'silver.reports',
                    'silver.spatial_features',
                    'silver.seismic_surveys',
                    'silver.raster_layers',
                    'silver.geophysics_surveys',
                    // RESTRICT / NO ACTION relations — would block the delete
                    'silver.mineral_claims',
                    'silver.review_queue',
                    'silver.campaigns',
                    'gold.zone_statistics',
                    'gold.element_correlations',
                ];
                foreach ($tables as $table) {
                    if (! $this->tableExists($table)) {
                        continue;
                    }

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
                'error' => SafeErrorMessage::forResponse($e),
            ], 500);
        }
    }

    /**
     * Whether $qualifiedTable ("schema.table" or, under SQLite, just
     * "table") actually exists in the connected database. Postgres-aware
     * (splits on the schema prefix); falls back to Laravel's driver-agnostic
     * Schema::hasTable() for SQLite, which has no schema concept.
     */
    private function tableExists(string $qualifiedTable): bool
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            $table = str_contains($qualifiedTable, '.')
                ? substr($qualifiedTable, strpos($qualifiedTable, '.') + 1)
                : $qualifiedTable;

            return Schema::hasTable($table);
        }

        [$schema, $table] = explode('.', $qualifiedTable, 2);

        return DB::table('information_schema.tables')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->exists();
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
                'error' => SafeErrorMessage::forResponse($e),
            ]);
        }
    }
}
