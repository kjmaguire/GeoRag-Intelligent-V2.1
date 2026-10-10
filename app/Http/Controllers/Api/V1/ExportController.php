<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Http\Requests\StoreExportRequest;
use App\Jobs\GenerateExportJob;
use App\Models\Export;
use App\Models\Project;
use App\Services\StorageService;
use App\Support\PaginationLimit;
use Carbon\CarbonInterface;
use Illuminate\Database\Eloquent\ModelNotFoundException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Throwable;

/**
 * Manages data export requests for a project.
 *
 * Routes (all under /api/v1):
 *   GET    /projects/{project}/exports           → index
 *   POST   /projects/{project}/exports           → store  (dispatches GenerateExportJob)
 *   GET    /projects/{project}/exports/{export}  → show
 *   GET    /exports/{export}/download            → download (302 redirect to signed URL)
 *
 * Download URLs are never stored. Every response that carries one mints it from
 * the row's `minio_path` with a short lifetime; see downloadLink().
 */
class ExportController extends Controller
{
    /**
     * Lifetime of a minted download URL.
     *
     * Short on purpose. A presigned URL stops working when the credentials that
     * signed it expire, whatever its X-Amz-Expires says, and production signs
     * with ECS task-role session credentials that last a few hours — a stored
     * 24-hour URL was dead long before its stated expiry. Minting per response
     * with a few minutes' life keeps the worst case to the SDK's one-minute
     * credential refresh window, and a client that finds a link dead asks for
     * another. The download route redirects at once, and S3 checks the expiry
     * when a request starts, so a long transfer that began in time finishes.
     */
    private const DOWNLOAD_URL_TTL_SECONDS = 300;

    public function __construct(
        private readonly StorageService $storage,
    ) {}

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
                ->paginate(PaginationLimit::clamp($request, 20));

            return response()->json(
                $exports->through(fn (Export $export): array => $this->present($export)),
            );
        } catch (ModelNotFoundException) {
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

            // silver.exports.workspace_id is NOT NULL with no default
            // (Tier C RLS migration, 97-rls-tenant-isolation-block2.sql)
            // and RLS requires it to match app.workspace_id. Without it
            // every insert here throws a NOT NULL violation.
            $export = Export::create([
                'project_id' => $project->project_id,
                'workspace_id' => $project->workspace_id,
                'export_type' => $request->validated('export_type'),
                'status' => 'pending',
                'filters' => $request->validated('filters') ?? [],
            ]);

            GenerateExportJob::dispatch($export->export_id);

            return response()->json([
                'data' => $export,
                'status_url' => route('api.projects.exports.show', [
                    'project' => $project->project_id,
                    'export' => $export->export_id,
                ]),
                'message' => 'Export job queued. Poll status_url until status is completed.',
            ], 202);
        } catch (ModelNotFoundException) {
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
     * Return the current status of an export, including a freshly minted
     * download URL when status is 'completed'.
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

            return response()->json(['data' => $this->present($export)]);
        } catch (ModelNotFoundException) {
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
     * Redirect to a presigned download URL for a completed export, minted for
     * this request from the stored object key.
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

            $link = $this->downloadLink($export);
            if ($link === null) {
                // Completed, yet no object key was recorded: nothing to sign.
                return response()->json(['message' => 'Export has no stored file.'], 404);
            }

            return redirect()->away($link['url']);
        } catch (ModelNotFoundException) {
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
        if ($user === null || ! $user->hasProjectAccess($projectId)) {
            return response()->json([
                'error' => 'forbidden',
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

    /**
     * The export as the API returns it: its columns plus a download link that
     * is minted for this response.
     *
     * `download_url` and `download_url_expires_at` are always present, null
     * until the export is completed, and never read from the row — see
     * {@see Export} for the legacy columns they used to come from.
     *
     * @return array<string, mixed>
     */
    private function present(Export $export): array
    {
        $link = $this->downloadLink($export);

        return array_merge($export->toArray(), [
            'download_url' => $link['url'] ?? null,
            'download_url_expires_at' => $link !== null ? $link['expires_at']->toJSON() : null,
        ]);
    }

    /**
     * Mint a short-lived URL for the export's file, or null when there is
     * nothing to download: the export is not completed, or no object key was
     * recorded for it.
     *
     * @return array{url: string, expires_at: CarbonInterface}|null
     */
    private function downloadLink(Export $export): ?array
    {
        $key = $export->minio_path;
        if ($export->status !== 'completed' || ! is_string($key) || $key === '') {
            return null;
        }

        $expiresAt = now()->addSeconds(self::DOWNLOAD_URL_TTL_SECONDS);

        return [
            'url' => $this->storage->presignedUrl($this->storage->exports(), $key, $expiresAt),
            'expires_at' => $expiresAt,
        ];
    }
}
