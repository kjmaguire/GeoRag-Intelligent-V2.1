<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Services\FastApiJwtMinter;
use App\Services\Ingestion\HatchetDispatchThrottle;
use App\Services\Ingestion\ShadowRouter;
use App\Services\StorageService;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;
use Throwable;

/**
 * File upload controller — uploads exploration data files to MinIO bronze bucket.
 *
 * Files land in the georag-bronze bucket under a category/project prefix.
 * PDF reports are dispatched directly to Hatchet's ingest_pdf workflow;
 * TIFF reports go through tiff_normalize first. Both triggers happen in the
 * upload request after the bronze manifest row is written.
 */
class UploadController extends Controller
{
    public function __construct(
        private readonly ShadowRouter $shadowRouter,
        private readonly HatchetDispatchThrottle $dispatchThrottle,
        private readonly StorageService $storage,
    ) {}

    /**
     * Accepted file categories and their MinIO path prefixes.
     *
     * Only categories with a live downstream consumer belong here. Both of
     * these dispatch a Hatchet workflow from store() below.
     */
    private const CATEGORIES = [
        // ADR-0005 (2026-05-23): TIFF scans normalize to PDF at the bronze
        // edge via tiff_normalize, then route through the §04p PDF stack
        // unchanged. Both extensions land under the same `reports/{project_id}/...`
        // prefix; dispatchShadowIfPdf() inspects the extension and calls
        // the right trigger endpoint.
        'reports' => ['pdf', 'tif', 'tiff'],
        // ZIP archives containing hundreds of small files (TIF, LAS, LOG,
        // XLSX, PDF ≤10 MB each). The Hatchet ingest_zip_archive workflow
        // extracts each entry and fans it out to the appropriate ingester.
        'archive' => ['zip'],

        // ── Restored 2026-08-20, each with a live Hatchet consumer ──────
        // These were retired on 2026-07-28 with the Dagster services and
        // answered 422 until now. The bar for being in this list is the one
        // RETIRED_CATEGORIES sets below: a workflow that actually runs.
        //
        // Drill data → ingest_tabular → silver.collars / surveys /
        // lithology_logs / samples. Collars are written before the interval
        // tables that reference them; the category name is passed through as
        // the sheet_type hint so a file with unusual headers still routes.
        'collars' => ['csv', 'txt', 'tsv'],
        'surveys' => ['csv', 'txt', 'tsv'],
        'lithology' => ['csv', 'txt', 'tsv'],
        'samples' => ['csv', 'txt', 'tsv'],
        // Workbooks → ingest_tabular, which classifies EVERY sheet rather
        // than assuming the first one is the data.
        'excel' => ['xlsx', 'xls', 'xlsm'],
        // Vector data + QGIS projects → ingest_spatial →
        // silver.spatial_features. `.zip` is here because a shapefile is
        // never one file: .shp/.shx/.dbf/.prj travel together, and a lone
        // .shp cannot be read without its siblings.
        'spatial' => ['geojson', 'json', 'shp', 'gpkg', 'gml', 'gpx', 'dxf', 'fgb', 'zip', 'qgs', 'qgz'],
        // LAS downhole curves -> ingest_well_logs -> silver.well_log_curves.
        // One row per CURVE with depth/value arrays, not a row per sample:
        // a 3,000 m hole logged every 15 cm is 20,000 samples per curve.
        'well_logs' => ['las'],
    ];

    /**
     * Categories retired with the Dagster services on 2026-07-28 (B2).
     *
     * These never reached a Hatchet workflow — store() dispatches only for
     * `reports` and `archive`. Everything else was picked up by Dagster's
     * minio_upload_sensor, which a live check confirmed was STOPPED: it
     * declares no default_status while every schedule in definitions.py
     * declares one, so it defaulted to STOPPED and never fired.
     *
     * The practical effect was that an upload in one of these categories
     * returned 201, wrote the object and a bronze manifest row, and then
     * nothing happened — no silver rows, no passages, no retrievable data,
     * and no error for the user to act on. Rejecting with an explicit 422 is
     * strictly better than accepting work we silently drop.
     *
     * Restoring any of these means giving it a live consumer first (a Hatchet
     * workflow, or Dagster brought back with the sensor actually RUNNING),
     * then moving the entry back into CATEGORIES.
     *
     * 2026-08-20: collars / surveys / lithology / samples / excel / spatial
     * met that bar and moved up into CATEGORIES — ingest_tabular and
     * ingest_spatial are registered in the Hatchet worker and have their own
     * trigger endpoints. The four below are still genuinely consumer-less and
     * stay here until they are not.
     *
     * @var array<string, list<string>>
     */
    private const RETIRED_CATEGORIES = [
        // Parsers exist and are tested for both, but neither has a workflow
        // or a settled silver table shape yet. Wiring them is the same shape
        // of work ingest_well_logs just did for LAS.
        'seismic' => ['sgy', 'segy'],
        'xyz' => ['xyz', 'dat', 'txt'],
        // Geophysics interpretation summary JSON — was consumed by the Dagster
        // silver_geophysics asset. No parser survives for it.
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

        // Answer retired categories with a reason rather than a bare "invalid
        // category" — these used to be accepted, so a caller hitting one is
        // most likely an older client, not a typo.
        $requestedCategory = $request->input('category');
        if (is_string($requestedCategory) && array_key_exists($requestedCategory, self::RETIRED_CATEGORIES)) {
            return response()->json([
                'message' => "Category '{$requestedCategory}' is no longer accepted. Its ingestion "
                    .'pipeline was retired on 2026-07-28; uploads were being stored but never '
                    .'processed. Accepted categories: '.implode(', ', array_keys(self::CATEGORIES)).'.',
                'retired_category' => $requestedCategory,
            ], 422);
        }

        $validated = $request->validate([
            'file' => ['required', 'file', 'max:6291456'], // 6 GB
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

            // Hash and upload in ONE pass over the file — the previous
            // shape did a full hash_file() read after the put, doubling
            // disk I/O on uploads capped at 6 GB.
            $handle = fopen($file->getRealPath(), 'r');
            if ($handle === false) {
                throw new \RuntimeException('Unable to open uploaded file for streaming.');
            }
            try {
                $hashCtx = hash_init('sha256');
                hash_update_stream($hashCtx, $handle);
                $sha256 = hash_final($hashCtx);
                rewind($handle);
                $this->storage->bronze()->put($minioKey, $handle, $putOptions);
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
                    // $sha256 computed in the streaming pass above.
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

            // Geology data — drill tables (CSV/XLSX) and vector/QGIS files.
            // Restored 2026-08-20; see GEOLOGY_WORKFLOWS.
            if (array_key_exists($category, self::GEOLOGY_WORKFLOWS)) {
                $this->dispatchGeologyIngest(
                    user: $user,
                    category: $category,
                    projectId: $projectId,
                    minioKey: $minioKey,
                    responseData: $responseData,
                );
            }

            // ZIP archive extraction — fan-out each contained file to the
            // appropriate ingester via the ingest_zip_archive Hatchet workflow.
            if ($category === 'archive' && $ext === 'zip') {
                $this->dispatchZipExtraction(
                    user: $user,
                    projectId: $projectId,
                    minioKey: $minioKey,
                    responseData: $responseData,
                );
            }

            // Mirrors DrillUploadController::store()'s fix for the same
            // class of bug: a category with a real dispatcher (reports,
            // archive) whose dispatch failed used to return 201 regardless,
            // with "dispatched": false buried in the body as the only
            // signal — read by callers as unqualified success while the
            // file silently never gets processed. The file IS stored
            // (bronze put + bronze.manifest row above); surface the
            // ingestion failure as a real error instead of a silent dead
            // end that only the nightly Tier-1 integrity sweep would catch.
            if (($responseData['ingest'] ?? null) !== null && $responseData['ingest']['dispatched'] === false) {
                $responseData['error'] = 'ingestion_dispatch_failed';

                return response()->json($responseData, 502);
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
     * If workspace lookup fails or the router throws, log + continue — this
     * dispatch below is the ONLY live path (see RETIRED_CATEGORIES above):
     * the Dagster minio_upload_sensor this docblock used to describe as a
     * fallback was confirmed STOPPED as of the 2026-07-28 (B2) trim.
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
                'SELECT CAST(workspace_id AS TEXT) AS workspace_id FROM silver.projects WHERE project_id = ?',
                [$projectId],
            );
            if ($row === null || empty($row->workspace_id)) {
                Log::info('UploadController: ingest skip — no workspace_id', [
                    'project_id' => $projectId,
                ]);
                $responseData['ingest'] = [
                    'dispatched' => false,
                    'reason' => 'no workspace_id for project',
                ];

                return;
            }
            $workspaceId = $row->workspace_id;

            $fastApiBase = rtrim(
                config('services.fastapi.internal_url')
                    ?? config('services.fastapi.internal_url'),
                '/',
            );
            $serviceKey = config('services.fastapi.service_key')
                ?? config('services.fastapi.service_key');
            if (! $serviceKey) {
                Log::warning('UploadController: FASTAPI_SERVICE_KEY missing — ingest not dispatched');
                $responseData['ingest'] = [
                    'dispatched' => false,
                    'reason' => 'FASTAPI_SERVICE_KEY not configured',
                ];

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

            // Throttle per-workspace before the trigger HTTP call so a
            // bulk upload can't saturate Hatchet's GROUP_ROUND_ROBIN
            // queue and lose the tail to silent CANCELLED events. The
            // tiff_normalize workflow also internally triggers ingest_pdf
            // against the same per-workspace concurrency group, so it
            // needs the same throttling as the direct PDF path.
            // See [[cameco-recovery-2026-06-02]].
            $this->dispatchThrottle->wait($workspaceId);

            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->retry(3, 500)->post(
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
            // Swallow the exception (never block the upload RESPONSE on
            // ingest plumbing failing) but still record it in $responseData
            // so store() can surface it — see the 502 check there.
            Log::warning('UploadController: ShadowRouter dispatch failed', [
                'project_id' => $projectId,
                'minio_key' => $minioKey,
                'error' => $e->getMessage(),
            ]);
            $responseData['ingest'] = [
                'dispatched' => false,
                'reason' => 'exception: '.$e->getMessage(),
            ];
        }
    }

    /**
     * Dispatch the ingest_zip_archive Hatchet workflow for a freshly-uploaded ZIP.
     *
     * Mirrors dispatchShadowIfPdf() — look up workspace_id, mint a JWT,
     * POST to FastAPI's internal trigger endpoint, and annotate $responseData.
     * Failures are swallowed so the upload response is never blocked.
     *
     * @param array<string, mixed> $responseData
     */
    private function dispatchZipExtraction(
        $user,
        string $projectId,
        string $minioKey,
        array &$responseData,
    ): void {
        try {
            $row = DB::selectOne(
                'SELECT CAST(workspace_id AS TEXT) AS workspace_id FROM silver.projects WHERE project_id = ?',
                [$projectId],
            );
            if ($row === null || empty($row->workspace_id)) {
                Log::info('UploadController: zip ingest skip — no workspace_id', [
                    'project_id' => $projectId,
                ]);
                $responseData['ingest'] = [
                    'dispatched' => false,
                    'reason' => 'no workspace_id for project',
                ];

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
                Log::warning('UploadController: FASTAPI_SERVICE_KEY missing — zip ingest not dispatched');
                $responseData['ingest'] = [
                    'dispatched' => false,
                    'reason' => 'FASTAPI_SERVICE_KEY not configured',
                ];

                return;
            }

            $jwt = app(FastApiJwtMinter::class)->mint(
                (string) ($user->id ?? 'unknown'),
                $projectId,
                [],
            );

            $runId = Str::uuid()->toString();
            $payload = [
                'workspace_id' => $workspaceId,
                'project_id' => $projectId,
                'minio_key' => $minioKey,
                'run_id' => $runId,
            ];

            // ZIP archives extract internally and fan out individual
            // ingest_pdf triggers, so a single zip upload can easily
            // saturate the workspace's Hatchet queue. Throttle the
            // initial dispatch the same way the PDF path does.
            $this->dispatchThrottle->wait($workspaceId);

            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->retry(3, 500)->post(
                $fastApiBase.'/internal/v1/shadow/ingest_zip_archive/trigger',
                $payload,
            );

            if ($resp->successful()) {
                $body = $resp->json();
                $responseData['ingest'] = [
                    'dispatched' => true,
                    'hatchet_workflow_run_id' => $body['hatchet_workflow_run_id'] ?? $body['workflow_run_id'] ?? null,
                    'run_id' => $runId,
                ];
                Log::info('UploadController: ingest_zip_archive dispatched', [
                    'workspace_id' => $workspaceId,
                    'project_id' => $projectId,
                    'minio_key' => $minioKey,
                    'workflow_run_id' => $body['hatchet_workflow_run_id'] ?? null,
                ]);
            } else {
                Log::warning('UploadController: ingest_zip_archive dispatch returned non-2xx', [
                    'status' => $resp->status(),
                    'body' => $resp->body(),
                ]);
                $responseData['ingest'] = [
                    'dispatched' => false,
                    'reason' => 'fastapi non-2xx '.$resp->status(),
                ];
            }
        } catch (Throwable $e) {
            Log::warning('UploadController: dispatchZipExtraction failed', [
                'project_id' => $projectId,
                'minio_key' => $minioKey,
                'error' => $e->getMessage(),
            ]);
            $responseData['ingest'] = [
                'dispatched' => false,
                'reason' => 'exception: '.$e->getMessage(),
            ];
        }
    }

    /**
     * Category → the Hatchet workflow that consumes it.
     *
     * Kept next to CATEGORIES on purpose: an entry there without an entry
     * here is precisely the failure RETIRED_CATEGORIES documents — a 201
     * with the object written and nothing downstream ever reading it.
     * dispatchGeologyIngest() refuses to dispatch a category it cannot map,
     * so the mistake surfaces as an explicit reason rather than as silence.
     *
     * @var array<string, string>
     */
    private const GEOLOGY_WORKFLOWS = [
        'collars' => 'ingest_tabular',
        'surveys' => 'ingest_tabular',
        'lithology' => 'ingest_tabular',
        'samples' => 'ingest_tabular',
        'excel' => 'ingest_tabular',
        'spatial' => 'ingest_spatial',
        'well_logs' => 'ingest_well_logs',
    ];

    /**
     * Dispatch a geology-data upload to ingest_tabular or ingest_spatial.
     *
     * Mirrors dispatchZipExtraction(): same JWT + X-Service-Key handshake,
     * same per-workspace throttle, same "never throw out of the upload
     * request" contract. A dispatch failure is recorded in the response body
     * and surfaced by the caller as a non-201, because a silent
     * `dispatched: false` read as success is the exact bug these categories
     * were retired over.
     */
    private function dispatchGeologyIngest(
        $user,
        string $category,
        string $projectId,
        string $minioKey,
        array &$responseData,
    ): void {
        try {
            $workflow = self::GEOLOGY_WORKFLOWS[$category] ?? null;
            if ($workflow === null) {
                Log::warning('UploadController: no workflow mapped for category', [
                    'category' => $category,
                ]);
                $responseData['ingest'] = [
                    'dispatched' => false,
                    'reason' => 'no workflow mapped for category '.$category,
                ];

                return;
            }

            $row = DB::selectOne(
                'SELECT CAST(workspace_id AS TEXT) AS workspace_id FROM silver.projects WHERE project_id = ?',
                [$projectId],
            );
            if ($row === null || empty($row->workspace_id)) {
                Log::info('UploadController: geology ingest skip — no workspace_id', [
                    'project_id' => $projectId,
                ]);
                $responseData['ingest'] = [
                    'dispatched' => false,
                    'reason' => 'no workspace_id for project',
                ];

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
                Log::warning('UploadController: FASTAPI_SERVICE_KEY missing — geology ingest not dispatched');
                $responseData['ingest'] = [
                    'dispatched' => false,
                    'reason' => 'FASTAPI_SERVICE_KEY not configured',
                ];

                return;
            }

            $jwt = app(FastApiJwtMinter::class)->mint(
                (string) ($user->id ?? 'unknown'),
                $projectId,
                [],
            );

            $runId = Str::uuid()->toString();
            $payload = [
                'workspace_id' => $workspaceId,
                'project_id' => $projectId,
                'minio_key' => $minioKey,
                'run_id' => $runId,
            ];

            // The category IS the sheet-type hint for a single-table CSV.
            // `excel` is deliberately excluded: a workbook holds several
            // tables, and pinning one type would make ingest_tabular treat
            // every sheet as that type instead of classifying each on its own.
            if ($workflow === 'ingest_tabular' && $category !== 'excel') {
                $payload['sheet_type'] = match ($category) {
                    'collars' => 'collar',
                    'surveys' => 'survey',
                    'lithology' => 'lithology',
                    'samples' => 'sample',
                    default => null,
                };
            }

            $this->dispatchThrottle->wait($workspaceId);

            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->retry(3, 500)->post(
                $fastApiBase.'/internal/v1/shadow/'.$workflow.'/trigger',
                $payload,
            );

            if ($resp->successful()) {
                $body = $resp->json();
                $responseData['ingest'] = [
                    'dispatched' => true,
                    'workflow' => $workflow,
                    'hatchet_workflow_run_id' => $body['workflow_run_id'] ?? null,
                    'run_id' => $runId,
                ];
                Log::info('UploadController: geology ingest dispatched', [
                    'workflow' => $workflow,
                    'category' => $category,
                    'workspace_id' => $workspaceId,
                    'minio_key' => $minioKey,
                ]);
            } else {
                Log::warning('UploadController: geology ingest returned non-2xx', [
                    'workflow' => $workflow,
                    'status' => $resp->status(),
                    'body' => $resp->body(),
                ]);
                $responseData['ingest'] = [
                    'dispatched' => false,
                    'reason' => 'fastapi non-2xx '.$resp->status(),
                ];
            }
        } catch (Throwable $e) {
            Log::warning('UploadController: dispatchGeologyIngest failed', [
                'category' => $category,
                'project_id' => $projectId,
                'minio_key' => $minioKey,
                'error' => $e->getMessage(),
            ]);
            $responseData['ingest'] = [
                'dispatched' => false,
                'reason' => 'exception: '.$e->getMessage(),
            ];
        }
    }

    /**
     * List accepted file categories and their extensions.
     *
     * GET /api/v1/upload/categories
     *
     * `retired` is returned alongside so a client can distinguish "we never
     * supported that" from "we stopped supporting that", and render the
     * picker without offering uploads that go nowhere.
     */
    public function categories(): JsonResponse
    {
        return response()->json([
            'categories' => self::CATEGORIES,
            'retired' => self::RETIRED_CATEGORIES,
        ]);
    }
}
