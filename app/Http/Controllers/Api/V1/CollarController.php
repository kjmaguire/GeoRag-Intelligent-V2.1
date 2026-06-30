<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Http\Requests\StoreCollarRequest;
use App\Http\Resources\CollarResource;
use App\Models\Collar;
use App\Models\Project;
use App\Support\AuthorizationAuditLogger;
use Illuminate\Database\Eloquent\ModelNotFoundException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Http\Resources\Json\AnonymousResourceCollection;
use Throwable;

class CollarController extends Controller
{
    /**
     * List collars for a project, paginated. Filterable by hole_type and status.
     *
     * GET /api/v1/projects/{project}/collars
     *
     * Returns 404 when the user is not a member of the parent project so we
     * do not leak project existence (existence oracle defence). The membership
     * check fires BEFORE the Project lookup.
     */
    public function index(Request $request, string $projectId): AnonymousResourceCollection|JsonResponse
    {
        // Gate: parent-project membership before any DB lookup.
        if (! $request->user()->hasProjectAccess($projectId)) {
            AuthorizationAuditLogger::deny(
                actor: $request->user(),
                targetResource: "project:{$projectId}",
                reason: 'no_pivot_row',
                context: ['action' => __FUNCTION__, 'path' => $request->path()],
            );

            return response()->json(['message' => 'Project not found.'], 404);
        }

        try {
            // Verify the parent project exists first so we return 404, not an empty list.
            $project = Project::findOrFail($projectId);

            $query = Collar::withCount(['surveys', 'samples'])
                ->selectRaw('*, ST_X(ST_Transform(geom, 4326)) AS longitude, ST_Y(ST_Transform(geom, 4326)) AS latitude')
                ->where('project_id', $project->project_id);

            if ($request->filled('hole_type')) {
                $query->where('hole_type', $request->string('hole_type'));
            }

            if ($request->filled('status')) {
                $query->where('status', $request->string('status'));
            }

            $collars = $query
                ->orderBy('hole_id')
                ->paginate($request->integer('per_page', 50));

            return CollarResource::collection($collars);
        } catch (ModelNotFoundException) {
            return response()->json(['message' => 'Project not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'message' => 'Failed to retrieve collars.',
                'error' => $e->getMessage(),
            ], 500);
        }
    }

    /**
     * Create a collar in a project.
     *
     * POST /api/v1/projects/{project}/collars
     *
     * Returns 404 when the user is not a member of the parent project.
     */
    public function store(StoreCollarRequest $request, string $projectId): JsonResponse
    {
        // Gate: parent-project membership before any DB lookup.
        if (! $request->user()->hasProjectAccess($projectId)) {
            AuthorizationAuditLogger::deny(
                actor: $request->user(),
                targetResource: "project:{$projectId}",
                reason: 'no_pivot_row',
                context: ['action' => __FUNCTION__, 'path' => $request->path()],
            );

            return response()->json(['message' => 'Project not found.'], 404);
        }

        try {
            $project = Project::findOrFail($projectId);

            $data = array_merge($request->validated(), [
                'project_id' => $project->project_id,
            ]);

            $collar = Collar::create($data);
            $collar->loadCount(['surveys', 'samples']);

            return (new CollarResource($collar))
                ->response()
                ->setStatusCode(201);
        } catch (ModelNotFoundException) {
            return response()->json(['message' => 'Project not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'message' => 'Failed to create collar.',
                'error' => $e->getMessage(),
            ], 500);
        }
    }

    /**
     * Show a single collar with all relationships loaded.
     *
     * GET /api/v1/projects/{project}/collars/{collar}
     *
     * Returns 404 when the user is not a member of the parent project.
     */
    public function show(Request $request, string $projectId, string $collarId): JsonResponse
    {
        // Gate: parent-project membership before any DB lookup.
        if (! $request->user()->hasProjectAccess($projectId)) {
            AuthorizationAuditLogger::deny(
                actor: $request->user(),
                targetResource: "project:{$projectId}",
                reason: 'no_pivot_row',
                context: ['action' => __FUNCTION__, 'path' => $request->path()],
            );

            return response()->json(['message' => 'Project not found.'], 404);
        }

        try {
            // Confirm the project exists to give a useful 404 if the project is wrong.
            Project::findOrFail($projectId);

            $collar = Collar::with([
                'surveys',
                'lithologyLogs',
                'alterations',
                'structures',
                'samples',
                'geochemistry',
                'wellLogCurves',
            ])
                ->withCount(['surveys', 'samples'])
                ->selectRaw('*, ST_X(ST_Transform(geom, 4326)) AS longitude, ST_Y(ST_Transform(geom, 4326)) AS latitude')
                ->where('project_id', $projectId)
                ->findOrFail($collarId);

            return (new CollarResource($collar))->response();
        } catch (ModelNotFoundException) {
            return response()->json(['message' => 'Collar not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'message' => 'Failed to retrieve collar.',
                'error' => $e->getMessage(),
            ], 500);
        }
    }

    /**
     * Delete a collar (cascades to surveys, lithology, samples, etc.).
     *
     * DELETE /api/v1/projects/{project}/collars/{collar}
     *
     * Returns 404 when the user is not a member of the parent project.
     */
    public function destroy(Request $request, string $projectId, string $collarId): JsonResponse
    {
        // Gate: parent-project membership before any DB lookup.
        if (! $request->user()->hasProjectAccess($projectId)) {
            AuthorizationAuditLogger::deny(
                actor: $request->user(),
                targetResource: "project:{$projectId}",
                reason: 'no_pivot_row',
                context: ['action' => __FUNCTION__, 'path' => $request->path()],
            );

            return response()->json(['message' => 'Project not found.'], 404);
        }

        try {
            Project::findOrFail($projectId);

            $collar = Collar::where('project_id', $projectId)
                ->findOrFail($collarId);

            $collar->delete();

            return response()->json(null, 204);
        } catch (ModelNotFoundException) {
            return response()->json(['message' => 'Collar not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'message' => 'Failed to delete collar.',
                'error' => $e->getMessage(),
            ], 500);
        }
    }
}
