<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Http\Requests\StoreExportRequest;
use App\Jobs\GenerateExportJob;
use App\Models\Export;
use App\Models\Project;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Storage;
use Throwable;

/**
 * Manages data export requests for a project.
 *
 * Routes (all under /api/v1):
 *   GET    /projects/{project}/exports           → index
 *   POST   /projects/{project}/exports           → store  (dispatches GenerateExportJob)
 *   GET    /projects/{project}/exports/{export}  → show
 *   GET    /exports/{export}/download            → download (302 redirect to signed URL)
 */
class ExportController extends Controller
{
    // -------------------------------------------------------------------------
    // index
    // -------------------------------------------------------------------------

    /**
     * List all exports for a project, newest first, paginated.
     *
     * GET /api/v1/projects/{project}/exports
     */
    public function index(Request $request, string $projectId): JsonResponse
    {
        try {
            if ($denied = $this->denyIfNoProjectAccess($request, $projectId)) {
                return $denied;
            }

            $project = Project::findOrFail($projectId);

            $exports = Export::where('project_id', $project->project_id)
                ->orderByDesc('created_at')
                ->paginate($request->integer('per_page', 20));

            return response()->json($exports);
        } catch (\Illuminate\Database\Eloquent\ModelNotFoundException) {
            return response()->json(['message' => 'Project not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return $this->serverError('Failed to list exports.', $e);
        }
    }

    // -------------------------------------------------------------------------
    // store
    // -------------------------------------------------------------------------

    /**
     * Create a new export job and dispatch it to Horizon.
     *
     * POST /api/v1/projects/{project}/exports
     * Returns 202 Accepted with the new Export record and a status polling URL.
     */
    public function store(StoreExportRequest $request, string $projectId): JsonResponse
    {
        try {
            if ($denied = $this->denyIfNoProjectAccess($request, $projectId)) {
                return $denied;
            }

            $project = Project::findOrFail($projectId);

            $export = Export::create([
                'project_id'  => $project->project_id,
                'export_type' => $request->validated('export_type'),
                'status'      => 'pending',
                'filters'     => $request->validated('filters') ?? [],
            ]);

            GenerateExportJob::dispatch($export->export_id);

            return response()->json([
                'data'        => $export,
                'status_url'  => route('api.projects.exports.show', [
                    'project' => $project->project_id,
                    'export'  => $export->export_id,
                ]),
                'message'     => 'Export job queued. Poll status_url until status is completed.',
            ], 202);
        } catch (\Illuminate\Database\Eloquent\ModelNotFoundException) {
            return response()->json(['message' => 'Project not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return $this->serverError('Failed to create export.', $e);
        }
    }

    // -------------------------------------------------------------------------
    // show
    // -------------------------------------------------------------------------

    /**
     * Return the current status of an export, including the download URL when
     * status is 'completed'.
     *
     * GET /api/v1/projects/{project}/exports/{export}
     */
    public function show(Request $request, string $projectId, string $exportId): JsonResponse
    {
        try {
            if ($denied = $this->denyIfNoProjectAccess($request, $projectId)) {
                return $denied;
            }

            Project::findOrFail($projectId);

            $export = Export::where('project_id', $projectId)
                ->findOrFail($exportId);

            // Refresh the signed URL if it has expired and the export is complete.
            if ($export->status === 'completed' && $this->urlExpired($export)) {
                $export = $this->refreshSignedUrl($export);
            }

            return response()->json(['data' => $export]);
        } catch (\Illuminate\Database\Eloquent\ModelNotFoundException) {
            return response()->json(['message' => 'Export not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return $this->serverError('Failed to retrieve export.', $e);
        }
    }

    // -------------------------------------------------------------------------
    // download
    // -------------------------------------------------------------------------

    /**
     * Redirect to the MinIO presigned download URL for a completed export.
     * Regenerates the URL if it has expired.
     *
     * GET /api/v1/exports/{export}/download
     */
    public function download(Request $request, string $exportId): RedirectResponse|JsonResponse
    {
        try {
            $export = Export::findOrFail($exportId);

            // Authorize on the project the export belongs to. Previously any
            // authenticated user with a valid export_id (UUID, but still
            // guessable via leaked logs) could fetch someone else's signed
            // download URL.
            if ($denied = $this->denyIfNoProjectAccess($request, (string) $export->project_id)) {
                return $denied;
            }

            if ($export->status !== 'completed') {
                return response()->json([
                    'message' => "Export is not ready. Current status: {$export->status}.",
                ], 409);
            }

            if ($this->urlExpired($export)) {
                $export = $this->refreshSignedUrl($export);
            }

            return redirect()->away($export->download_url);
        } catch (\Illuminate\Database\Eloquent\ModelNotFoundException) {
            return response()->json(['message' => 'Export not found.'], 404);
        } catch (Throwable $e) {
            report($e);

            return $this->serverError('Failed to generate download URL.', $e);
        }
    }

    // -------------------------------------------------------------------------
    // Private helpers
    // -------------------------------------------------------------------------

    /**
     * Return a 403 JsonResponse if the authenticated user does not have
     * access to the project, or null if they do. Used as a guard at the top
     * of every action so the legitimate 404 path (project doesn't exist)
     * stays distinguishable from the forbidden path (project exists but the
     * user isn't a member).
     */
    private function denyIfNoProjectAccess(Request $request, string $projectId): ?JsonResponse
    {
        $user = $request->user();
        if ($user === null || !$user->hasProjectAccess($projectId)) {
            return response()->json([
                'error'   => 'forbidden',
                'message' => 'You do not have access to this project.',
            ], 403);
        }
        return null;
    }

    /**
     * Build a server-error JSON response that only discloses the underlying
     * exception message when APP_DEBUG is on. In production the client just
     * sees the generic message so driver/credential metadata can't leak via
     * stack-derived error strings (e.g. Flysystem/S3 error bodies).
     */
    private function serverError(string $message, Throwable $e): JsonResponse
    {
        $body = ['message' => $message];
        if (config('app.debug')) {
            $body['error'] = $e->getMessage();
        }
        return response()->json($body, 500);
    }

    private function urlExpired(Export $export): bool
    {
        if (!$export->download_url || !$export->download_url_expires_at) {
            return true;
        }

        // Treat as expired if within 5 minutes of the expiry to avoid race conditions.
        return $export->download_url_expires_at->subMinutes(5)->isPast();
    }

    private function refreshSignedUrl(Export $export): Export
    {
        $expiresAt = now()->addHours(24);
        $signedUrl = Storage::disk('s3-exports')->temporaryUrl($export->minio_path, $expiresAt);

        $export->update([
            'download_url'            => $signedUrl,
            'download_url_expires_at' => $expiresAt,
        ]);

        return $export->fresh();
    }
}
