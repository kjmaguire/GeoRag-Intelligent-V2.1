<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Enums\CollarStatus;
use App\Http\Controllers\Controller;
use App\Http\Requests\StoreCollarRequest;
use App\Http\Resources\CollarResource;
use App\Models\Collar;
use App\Models\Project;
use App\Support\AuthorizationAuditLogger;
use App\Support\HoleId;
use App\Support\PaginationLimit;
use App\Support\SafeErrorMessage;
use Illuminate\Database\Eloquent\ModelNotFoundException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Http\Resources\Json\AnonymousResourceCollection;
use Illuminate\Support\Arr;
use Illuminate\Support\Facades\DB;
use Throwable;

class CollarController extends Controller
{
    /**
     * List collars for a project, paginated. Filterable by hole_type, status
     * and hole_id (exact match on the display id or its canonical form, so
     * `LEB-23-001` and `leb 23 001` both find the same collar).
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

        // Validated outside the try block below, which would turn the
        // ValidationException into a 500.
        $request->validate([
            'hole_id' => ['sometimes', 'nullable', 'string', 'max:50'],
        ]);

        try {
            // Verify the parent project exists first so we return 404, not an empty list.
            $project = Project::findOrFail($projectId);

            $query = Collar::withCount(['surveys', 'samples'])
                ->selectRaw('*, ST_X(geom_4326) AS longitude, ST_Y(geom_4326) AS latitude')
                ->where('project_id', $project->project_id);

            if ($request->filled('hole_type')) {
                $query->where('hole_type', $request->string('hole_type'));
            }

            if ($request->filled('status')) {
                $query->where('status', $request->string('status'));
            }

            if ($request->filled('hole_id')) {
                $holeId = (string) $request->input('hole_id');
                $canonical = HoleId::canonicalize($holeId);

                $query->where(function ($match) use ($holeId, $canonical): void {
                    $match->where('hole_id', $holeId);

                    if ($canonical !== null) {
                        $match->orWhere('hole_id_canonical', $canonical);
                    }
                });
            }

            $collars = $query
                ->orderBy('hole_id')
                ->paginate(PaginationLimit::clamp($request, 50));

            return CollarResource::collection($collars);
        } catch (ModelNotFoundException) {
            return response()->json(['message' => 'Project not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'message' => 'Failed to retrieve collars.',
                'error' => SafeErrorMessage::forResponse($e),
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

            // Only geom_4326 places a collar: the tile source, the traces,
            // the agent tools and the project hull all read it, and nothing
            // derives it any more (2026_09_30_100000 dropped the trigger).
            // Easting/northing on their own are numbers in an unknown frame,
            // so a collar with no coordinate system is refused rather than
            // stored where nothing can find it. The project's crs_datum is
            // NOT used: every project is created with 'EPSG:32613' there,
            // whatever its real system is (Project::effectiveCrsEpsg()).
            $epsg = $request->validated('crs_epsg') ?? $project->crs_epsg;
            if ($epsg === null || ! $this->isKnownSrid((int) $epsg)) {
                return response()->json([
                    'message' => 'A known coordinate system is needed to place this collar: send crs_epsg, or set one on the project.',
                    'errors' => ['crs_epsg' => ['A known EPSG code is required when the project has none.']],
                ], 422);
            }

            $data = array_merge(Arr::except($request->validated(), ['crs_epsg']), [
                'project_id' => $project->project_id,
                // The database derives it too (trg_collars_hole_id_canonical);
                // set here so the model and any non-Postgres test DB agree.
                'hole_id_canonical' => HoleId::canonicalize((string) $request->validated('hole_id')),
                // NOT NULL in silver.collars; 'unknown' is what ingest writes
                // when a file has no status (silver_row_guard).
                'status' => $request->validated('status') ?? CollarStatus::Unknown->value,
                // Typed in by hand against a stated coordinate system.
                'georef_method' => 'manual',
            ]);

            $collar = DB::transaction(function () use ($data, $project, $epsg): Collar {
                $collar = new Collar($data);
                // Not fillable, and NOT NULL once the raw RLS block has run.
                $collar->forceFill(['workspace_id' => $project->workspace_id]);
                $collar->save();

                if (DB::connection()->getDriverName() === 'pgsql') {
                    DB::update(
                        'UPDATE silver.collars
                            SET geom_4326 = ST_Transform(ST_SetSRID(ST_MakePoint(?, ?), ?::int), 4326)
                          WHERE collar_id = ?::uuid',
                        [(float) $data['easting'], (float) $data['northing'], (int) $epsg, $collar->collar_id],
                    );
                }

                return $collar;
            });
            $collar->refresh();
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
                'error' => SafeErrorMessage::forResponse($e),
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

            // Only what CollarResource serialises. wellLogCurves used to be
            // eager-loaded here too, though the resource never emits it: each
            // curve carries two float8[] arrays (every depth and every value),
            // so this read megabytes per hole to throw them away.
            $collar = Collar::with([
                'surveys',
                'lithologyLogs',
                'alterations',
                'mineralization',
                'structures',
                'samples',
                'geochemistry',
            ])
                ->withCount(['surveys', 'samples'])
                ->selectRaw('*, ST_X(geom_4326) AS longitude, ST_Y(geom_4326) AS latitude')
                ->where('project_id', $projectId)
                ->findOrFail($collarId);

            return (new CollarResource($collar))->response();
        } catch (ModelNotFoundException) {
            return response()->json(['message' => 'Collar not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return response()->json([
                'message' => 'Failed to retrieve collar.',
                'error' => SafeErrorMessage::forResponse($e),
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
                'error' => SafeErrorMessage::forResponse($e),
            ], 500);
        }
    }

    /**
     * Whether PostGIS can transform from this SRID. ST_Transform on an
     * unknown one raises, which would surface as a 500 for a typo.
     * SQLite (the fast test suite) has no spatial_ref_sys: any code passes.
     */
    private function isKnownSrid(int $srid): bool
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return true;
        }

        return DB::table('spatial_ref_sys')->where('srid', $srid)->exists();
    }
}
