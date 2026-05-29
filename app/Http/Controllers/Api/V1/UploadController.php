<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Services\FastApiJwtMinter;
use App\Services\Ingestion\ShadowRouter;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Throwable;

/**
 * File upload controller — uploads exploration data files to MinIO bronze bucket.
 *
 * Uploaded files land in the georag-bronze bucket under a path prefix that
 * maps to the Dagster sensor's _PREFIX_TO_ASSET mapping:
 *   collars/   → bronze_collars
 *   surveys/   → bronze_surveys
 *   lithology/ → bronze_lithology
 *   samples/   → bronze_samples
 *   reports/   → bronze_reports
 *   well_logs/ → bronze_well_logs
 *   spatial/   → bronze_spatial
 *   excel/     → bronze_xlsx
 *   seismic/   → bronze_seismic
 *   xyz/       → bronze_xyz
 *
 * The Dagster MinIO sensor polls every 5 minutes and triggers the relevant
 * Bronze asset when new files are detected.
 */
class UploadController extends Controller
{
    public function __construct(
        private readonly ShadowRouter $shadowRouter,
    ) {}

    /**
     * Accepted file categories and their MinIO path prefixes.
     */
    private const CATEGORIES = [
        'collars' => ['csv'],
        'surveys' => ['csv'],
        'lithology' => ['csv'],
        'samples' => ['csv'],
        // ADR-0005 (2026-05-23): TIFF scans normalize to PDF at the bronze
        // edge via tiff_normalize, then route through the §04p PDF stack
        // unchanged. Both extensions land under the same `reports/{project_id}/...`
        // prefix; dispatchShadowIfPdf() inspects the extension and calls
        // the right trigger endpoint.
        'reports' => ['pdf', 'tif', 'tiff'],
        'well_logs' => ['las'],
        'spatial' => ['geojson', 'shp', 'zip'],
        'excel' => ['xlsx', 'xls'],
        'seismic' => ['sgy', 'segy'],
        'xyz' => ['xyz', 'dat', 'txt'],
        // Geophysics interpretation summary JSON — consumed by Dagster
        // silver_geophysics asset. Schema documented in src/dagster/.../bronze_geophysics.py.
        'geophysics' => ['json'],
    ];

    /**
     * Upload a file to the MinIO bronze bucket.
     *
     * POST /api/v1/projects/{project}/upload
     *
     * Form data:
     *   file      — the file (required, max 100 MB)
     *   category  — one of the CATEGORIES keys (required)
     */
    public function store(Request $request, string $projectId): JsonResponse
    {
        // ── Authorization ────────────────────────────────────────────────
        // Any authenticated user could previously upload into any project's
        // MinIO prefix just by swapping the URL parameter. Gate strictly on
        // project_user membership. Returning 403 (not 404) is deliberate:
        // we already passed auth and the project id is structurally valid.
        $user = $request->user();
        if ($user === null || ! $user->hasProjectAccess($projectId)) {
            return response()->json([
                'error' => 'forbidden',
                'message' => 'You do not have access to this project.',
            ], 403);
        }

        $validated = $request->validate([
            'file' => ['required', 'file', 'max:2097152'], // 2 GB
            'category' => ['required', 'string', 'in:'.implode(',', array_keys(self::CATEGORIES))],
            'vendor_profile_id' => ['nullable', 'integer', 'exists:vendor_profiles,id'],
        ]);

        $file = $request->file('file');
        $category = $validated['category'];
        $vendorProfileId = $validated['vendor_profile_id'] ?? null;

        // Validate file extension against category
        $ext = strtolower($file->getClientOriginalExtension());
        $allowedExts = self::CATEGORIES[$category];
        if (! in_array($ext, $allowedExts, true)) {
            return response()->json([
                'message' => "Invalid file extension '.{$ext}' for category '{$category}'. Allowed: ".implode(', ', $allowedExts),
            ], 422);
        }

        // ── Filename sanitization ────────────────────────────────────────
        // The previous code wrote `{$category}/{$filename}` using the raw
        // client-supplied name, which allows:
        //   - path traversal ("../../other/category/foo.csv")
        //   - collisions across users/projects ("collars.csv")
        //   - arbitrary control characters / embedded nulls
        //
        // We now strip path components, collapse disallowed characters to
        // underscores, truncate to a sane length, and prefix with the
        // project_id + a timestamp so concurrent uploads of the same logical
        // filename from different projects never clobber each other.
        $originalName = $file->getClientOriginalName();
        $safeBase = pathinfo($originalName, PATHINFO_FILENAME);
        $safeBase = preg_replace('/[^A-Za-z0-9._-]+/', '_', $safeBase) ?? 'upload';
        $safeBase = trim($safeBase, '._-') ?: 'upload';
        $safeBase = substr($safeBase, 0, 120);
        $safeFilename = $safeBase.'.'.$ext;

        // ADR-0005 (2026-05-23): TIFF scans live under their own `tiff/`
        // MinIO prefix even though they share the `reports` category in
        // the API. This keeps the existing bronze `reports/` sensor
        // pointing at PDFs only; the TIFF normalise workflow takes the
        // `tiff/` traffic, derives a PDF under `reports/`, and triggers
        // ingest_pdf. The category remains `reports` for UX so the user
        // doesn't have to know about the format-routing detail.
        $keyPrefix = $category;
        if ($category === 'reports' && in_array($ext, ['tif', 'tiff'], true)) {
            $keyPrefix = 'tiff';
        }
        $minioKey = sprintf(
            '%s/%s/%s_%s',
            $keyPrefix,
            $projectId,
            now()->format('Ymd_His'),
            $safeFilename,
        );

        try {
            // Upload to MinIO bronze bucket via the s3 disk. Use putStream
            // so we don't buffer large files into PHP memory.
            //
            // When a vendor_profile_id is supplied we attach it as S3 object
            // metadata so the ingestion parser (Phase 2) can look up the
            // correct column mapping without needing to consult Laravel.
            // Laravel only records the ID — mapping resolution is the
            // parser's job.
            $putOptions = [];
            if ($vendorProfileId !== null) {
                $putOptions['Metadata'] = [
                    'x-georag-vendor-profile-id' => (string) $vendorProfileId,
                ];
            }

            $handle = fopen($file->getRealPath(), 'r');
            if ($handle === false) {
                throw new \RuntimeException('Unable to open uploaded file for streaming.');
            }
            try {
                Storage::disk('s3')->put($minioKey, $handle, $putOptions);
            } finally {
                if (is_resource($handle)) {
                    fclose($handle);
                }
            }

            // Reliability spec — bronze.manifest population. Synchronous,
            // before any Hatchet dispatch, so the nightly Tier 1
            // integrity sweep can detect orphaned uploads (bronze rows
            // with no corresponding silver.reports entry). sha256 is
            // computed once here; UNIQUE (workspace_id, file_key) makes
            // this idempotent if the request retries.
            try {
                $workspaceId = Project::query()
                    ->where('project_id', $projectId)
                    ->value('workspace_id');
                if ($workspaceId !== null) {
                    $sha256 = hash_file('sha256', $file->getRealPath()) ?: '';
                    DB::statement(
                        'INSERT INTO bronze.manifest
                             (file_key, workspace_id, sha256, document_type,
                              uploaded_at, dispatch_attempts)
                         VALUES (?, ?::uuid, ?, ?, NOW(), 0)
                         ON CONFLICT (workspace_id, file_key) DO NOTHING',
                        [$minioKey, $workspaceId, $sha256, $category],
                    );
                }
            } catch (Throwable $manifestExc) {
                // Manifest write is best-effort — a failure here must NOT
                // block the user's upload from proceeding to ingest. Tier
                // 1 will just have one fewer row to audit; the existing
                // silver.reports row remains the source of truth for the
                // happy path.
                Log::warning('UploadController: bronze.manifest insert failed', [
                    'minio_key' => $minioKey,
                    'error' => $manifestExc->getMessage(),
                ]);
            }

            Log::info('UploadController: file uploaded', [
                'project_id' => $projectId,
                'user_id' => $user->id,
                'category' => $category,
                'minio_key' => $minioKey,
                'original_filename' => $originalName,
                'size' => $file->getSize(),
                'vendor_profile_id' => $vendorProfileId,
            ]);

            $responseData = [
                'message' => 'File uploaded successfully. The ingestion pipeline will process it within 5 minutes.',
                'minio_key' => $minioKey,
                'size' => $file->getSize(),
                'category' => $category,
            ];

            if ($vendorProfileId !== null) {
                $responseData['vendor_profile_id'] = $vendorProfileId;
            }

            // Phase 1 Step 5 — for PDF reports, optionally dual-write to the
            // Hatchet ingest_pdf workflow. ShadowRouter consults the
            // workspace + platform feature flags and decides per-upload; on
            // 'single' (the default until traffic_pct > 0) this is a no-op.
            //
            // ADR-0005 (2026-05-23): TIFF scans under the same `reports`
            // category route to the tiff_normalize Hatchet workflow instead;
            // it wraps the TIFF to PDF, lands the derived PDF under
            // `bronze/reports/...`, and internally triggers ingest_pdf.
            if ($category === 'reports') {
                $this->dispatchShadowIfPdf(
                    user: $user,
                    projectId: $projectId,
                    minioKey: $minioKey,
                    fileSize: (int) $file->getSize(),
                    vendorProfileId: $vendorProfileId,
                    responseData: $responseData,
                    isTiff: in_array($ext, ['tif', 'tiff'], true),
                );
            }

            return response()->json($responseData, 201);
        } catch (Throwable $e) {
            Log::error('UploadController: upload failed', [
                'project_id' => $projectId,
                'error' => $e->getMessage(),
            ]);

            // Do NOT leak the exception message to the client unless in debug
            // mode — storage driver errors can disclose internal endpoint
            // URLs / credentials / region metadata.
            $response = ['message' => 'File upload failed.'];
            if (config('app.debug')) {
                $response['error'] = $e->getMessage();
            }

            return response()->json($response, 500);
        }
    }

    /**
     * Resolve the workspace_id for this project_id and pass to the ShadowRouter.
     *
     * If workspace lookup fails or the router throws, log + continue — the
     * v1.49 Dagster path still runs from the bronze upload sensor regardless.
     *
     * @param array<string, mixed> $responseData
     */
    private function dispatchShadowIfPdf(
        $user,
        string $projectId,
        string $minioKey,
        int $fileSize,
        ?int $vendorProfileId,
        array &$responseData,
        bool $isTiff = false,
    ): void {
        // Post-Phase-4: the shadow_runs table is gone (Phase 1 ramp ended).
        // Dispatch ingest_pdf directly via FastAPI's /internal/v1/shadow/
        // ingest_pdf/trigger endpoint, bypassing the retired ShadowRouter.
        try {
            $row = DB::selectOne(
                'SELECT workspace_id::text AS workspace_id FROM silver.projects WHERE project_id = ?',
                [$projectId],
            );
            if ($row === null || empty($row->workspace_id)) {
                Log::info('UploadController: ingest skip — no workspace_id', [
                    'project_id' => $projectId,
                ]);

                return;
            }
            $workspaceId = $row->workspace_id;

            $fastApiBase = rtrim(
                config('services.fastapi.internal_url')
                    ?? env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'),
                '/',
            );
            $serviceKey = config('services.fastapi.service_key')
                ?? env('FASTAPI_SERVICE_KEY');
            if (! $serviceKey) {
                Log::warning('UploadController: FASTAPI_SERVICE_KEY missing — ingest not dispatched');

                return;
            }

            // Mint a per-user JWT so FastAPI's auth layer accepts the call.
            $jwt = app(FastApiJwtMinter::class)->mint(
                (string) ($user->id ?? 'unknown'),
                $projectId,
                [],
            );

            $payload = [
                'workspace_id' => $workspaceId,
                'project_id' => $projectId,
                'minio_key' => $minioKey,
                'file_size' => $fileSize,
                'vendor_profile_id' => $vendorProfileId,
                'correlation_token' => 'upload-'.Str::uuid()->toString(),
            ];

            // ADR-0005: TIFF uploads route to the normalize endpoint;
            // PDF uploads keep the direct ingest_pdf path. Both return
            // a workflow_run_id + correlation_token on 202.
            $triggerPath = $isTiff
                ? '/internal/v1/shadow/tiff_normalize/trigger'
                : '/internal/v1/shadow/ingest_pdf/trigger';

            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->post(
                $fastApiBase.$triggerPath,
                $payload,
            );

            // FastAPI returns 202 Accepted on successful dispatch (not 200).
            if ($resp->successful()) {
                $body = $resp->json();
                $responseData['ingest'] = [
                    'dispatched' => true,
                    'hatchet_workflow_run_id' => $body['hatchet_workflow_run_id'] ?? $body['workflow_run_id'] ?? null,
                    'correlation_token' => $payload['correlation_token'],
                ];
                Log::info('UploadController: ingest_pdf dispatched', [
                    'workspace_id' => $workspaceId,
                    'project_id' => $projectId,
                    'workflow_run_id' => $body['hatchet_workflow_run_id'] ?? null,
                ]);
            } else {
                Log::warning('UploadController: ingest_pdf dispatch returned non-2xx', [
                    'status' => $resp->status(),
                    'body' => $resp->body(),
                ]);
                $responseData['ingest'] = [
                    'dispatched' => false,
                    'reason' => 'fastapi non-2xx '.$resp->status(),
                ];
            }
        } catch (Throwable $e) {
            // Swallow — never block the upload response on ingest plumbing.
            Log::warning('UploadController: ShadowRouter dispatch failed', [
                'project_id' => $projectId,
                'minio_key' => $minioKey,
                'error' => $e->getMessage(),
            ]);
        }
    }

    /**
     * List accepted file categories and their extensions.
     *
     * GET /api/v1/upload/categories
     */
    public function categories(): JsonResponse
    {
        return response()->json([
            'categories' => self::CATEGORIES,
        ]);
    }
}
