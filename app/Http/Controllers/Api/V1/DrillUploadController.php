<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Models\User;
use App\Services\FastApiJwtMinter;
use App\Services\Ingestion\DrillFileRouter;
use App\Services\Ingestion\HatchetDispatchThrottle;
use App\Services\StorageService;
use App\Support\SafeErrorMessage;
use App\Support\UploadContentGuard;
use App\Support\Uploads;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;
use Throwable;

/**
 * Drill-data upload — Slice 1 of CC-01 Item 1.
 *
 * POST /api/v1/projects/{slug}/drill-uploads
 *
 * Distinct from {@see UploadController} in three ways:
 *   1. Slug-based routing — matches Foundry's slug-scoped URLs.
 *   2. Writes a bronze.source_files row so the SRQ + lineage chain has
 *      an anchor (UploadController never persisted provenance).
 *   3. Dispatches the matching Hatchet workflow synchronously
 *      instead of waiting 5 minutes for an object-store sensor poll.
 *
 * Until 2026-08-22 this rejected every non-PDF extension with a 422
 * blaming the 2026-07-28 Dagster retirement. `ingest_tabular` shipped
 * on 2026-08-20 and UploadController restored the same formats the
 * same day, so the two surfaces spent two days disagreeing about
 * whether the platform accepts a collar CSV -- with the drill-specific
 * one being the half that refused drill data.
 *
 * The two controllers intentionally do not share code in v1; the
 * generic /upload flow serves the data-import wizard, this one serves
 * the drill-review-first UX. Future consolidation is documented in CC-01.
 */
class DrillUploadController extends Controller
{
    /**
     * EPSG code bounds for the `source_epsg` override.
     *
     * Named rather than inlined for two reasons. It is the same range the
     * database already enforces (chk_spatial_features_crs_native, and the
     * matching CHECK on silver.geophysics_surveys.crs_epsg), so a single
     * symbol keeps the two definitions visibly paired. And
     * UploadSizeCapConsistencyTest greps this file for a literal
     * `'max:<5+ digits>'` rule to stop a hand-written FILE SIZE cap creeping
     * back in; 32767 is a coordinate-system identifier, not a size, and it
     * should not have to weaken that guard to coexist with it.
     */
    private const EPSG_MIN = 1024;

    private const EPSG_MAX = 32767;

    /** Bronze object-key prefix; workspace-scoped to keep multi-tenant blast radius tight. */
    private const BRONZE_PREFIX = 'drill-uploads';

    private const ALLOWED_EXTS = ['csv', 'xlsx', 'xls', 'pdf'];

    /** Octane-safe: no constructor state — resolve services per request. */
    public function store(Request $request, string $slug, StorageService $storage): JsonResponse
    {
        $project = Project::where('slug', $slug)->first();
        if ($project === null) {
            return response()->json(['error' => 'not_found'], 404);
        }

        $user = $request->user();
        if ($user === null || ! $user->hasProjectAccess($project->project_id)) {
            return response()->json(['error' => 'forbidden'], 403);
        }

        $validated = $request->validate([
            // See App\Support\Uploads. This was `max:2097152` — 2 GiB, the
            // whole memory allocation of the container serving the request.
            'file' => ['required', 'file', 'max:'.Uploads::maxKilobytes()],
            'vendor_profile_id' => ['nullable', 'integer', 'exists:vendor_profiles,id'],
            // Operator-supplied CRS for the collar coordinates in this file.
            // Same rule and same wire type as UploadController and
            // StoreQueryRequest: an EPSG integer, never a CRS string,
            // bounded by the DB CHECK on the columns it lands in.
            //
            // Wiring this surface is not optional. ingest_tabular has always
            // accepted source_epsg and has never once been sent one, so every
            // drill file uploaded here has silently taken its
            // DEFAULT_SOURCE_EPSG of 32613 (UTM 13N). An override wired only
            // into the wizard's /upload route would have left this one
            // guessing.
            'source_epsg' => ['nullable', 'integer', 'min:'.self::EPSG_MIN, 'max:'.self::EPSG_MAX],
        ], [
            // Inline validate() has no messages() to hang these on; keep the
            // wording identical to StoreQueryRequest::messages().
            'source_epsg.min' => 'EPSG codes must be in the range 1024-32767.',
            'source_epsg.max' => 'EPSG codes must be in the range 1024-32767.',
        ]);

        $file = $request->file('file');
        $ext = strtolower($file->getClientOriginalExtension());
        if (! in_array($ext, self::ALLOWED_EXTS, true)) {
            return response()->json([
                'error' => 'unsupported_extension',
                'message' => "Extension '.{$ext}' not supported. Allowed: ".implode(', ', self::ALLOWED_EXTS),
            ], 422);
        }

        // This controller already sniffed the real MIME type further down —
        // and only ever persisted it to bronze.source_files. Checking it
        // here means the contradiction is caught before anything is stored
        // or dispatched, rather than recorded alongside the file it
        // describes. Lenient by design; see UploadContentGuard.
        try {
            $sniffedType = $file->getMimeType();
        } catch (Throwable) {
            $sniffedType = null;
        }
        if (UploadContentGuard::mimeMismatch($ext, $sniffedType)) {
            return response()->json([
                'error' => 'content_type_mismatch',
                'message' => "This file is named '.{$ext}' but its contents are "
                    ."'{$sniffedType}'. Rename it to match the real format.",
            ], 422);
        }

        $workspaceId = $this->workspaceIdFor($project->project_id);
        if ($workspaceId === null) {
            return response()->json(['error' => 'workspace_unresolved'], 500);
        }

        $originalName = $file->getClientOriginalName();
        // Single streaming pass for the hash — hash_file + a later fresh
        // fopen + a FileinfoMimeTypeGuesser walk read a multi-GB upload
        // three times over; the hash must still come FIRST because the
        // dedupe SELECT below needs it before anything is written.
        $handle = fopen($file->getRealPath(), 'rb');
        if ($handle === false) {
            return response()->json(['error' => 'sha_compute_failed'], 500);
        }
        $hashCtx = hash_init('sha256');
        hash_update_stream($hashCtx, $handle);
        $sha256 = hash_final($hashCtx);
        fclose($handle);

        // Dedupe early — same workspace + same content = same row. This
        // protects against accidental double-uploads from the UI without
        // forcing the client to track upload ids.
        $existing = DB::table('bronze.source_files')
            ->where('workspace_id', $workspaceId)
            ->where('file_sha256', $sha256)
            ->first();
        if ($existing !== null) {
            return response()->json([
                'duplicate' => true,
                'source_file_id' => $existing->id,
                'seaweedfs_key' => $existing->seaweedfs_key,
                'message' => 'File with this SHA256 already ingested for this workspace.',
            ], 200);
        }

        $safeFilename = $this->safeFilename($originalName, $ext);
        $shortSha = substr($sha256, 0, 8);
        $seaweedfsKey = sprintf(
            '%s/%s/%s_%s_%s',
            self::BRONZE_PREFIX,
            $workspaceId,
            now()->format('Ymd_His'),
            $shortSha,
            $safeFilename,
        );

        $vendorProfileId = $validated['vendor_profile_id'] ?? null;
        $sourceEpsg = $validated['source_epsg'] ?? null;

        try {
            $this->streamToBronze($storage, $seaweedfsKey, $file->getRealPath(), $vendorProfileId);
        } catch (Throwable $e) {
            Log::error('DrillUploadController: bronze write failed', [
                'project_id' => $project->project_id,
                'workspace_id' => $workspaceId,
                'error' => SafeErrorMessage::forResponse($e),
            ]);

            return response()->json([
                'error' => 'bronze_write_failed',
                'message' => config('app.debug') ? $e->getMessage() : null,
            ], 500);
        }

        $selection = DrillFileRouter::select($ext, $originalName);

        // Security fix 2026-08-14 (MED): persist the server-sniffed MIME, not
        // the client-declared one — the client value is attacker-controlled.
        // finfo only inspects the file's leading magic bytes (not a full
        // re-read), so the multi-GB concern that originally justified
        // getClientMimeType() does not apply. Fall back to the client mime
        // only if sniffing fails; column contract (string) is unchanged.
        try {
            $mimeType = $file->getMimeType();
        } catch (Throwable) {
            $mimeType = null;
        }
        $mimeType = $mimeType ?: $file->getClientMimeType();

        $sourceFileId = (string) Str::uuid();
        try {
            DB::table('bronze.source_files')->insert([
                'id' => $sourceFileId,
                'workspace_id' => $workspaceId,
                'seaweedfs_key' => $seaweedfsKey,
                'original_filename' => $originalName,
                'file_sha256' => $sha256,
                'file_size_bytes' => $file->getSize(),
                'mime_type' => $mimeType,
                'source_type' => 'drill_upload',
                // The sheet-type hint, or null when the filename gave
                // none and ingest_tabular will classify from the header
                // row. 'unrouted' is reserved for an extension with no
                // workflow at all — the two are not the same, and
                // recording them alike hid which files were dispatched.
                'data_type' => $selection['sheet_type'] ?? $selection['route'],
                'campaign_id' => null,
                'ingested_by' => (string) $user->id,
                'ingested_at' => now(),
            ]);
        } catch (Throwable $e) {
            // The object was written before the row — on any branch that
            // exits without a row referencing $seaweedfsKey, delete it or
            // it becomes invisible unbounded storage growth (the Tier-1
            // sweep audits rows, not objects).
            try {
                $storage->bronze()->delete($seaweedfsKey);
            } catch (Throwable) {
                // Best-effort; orphan is logged below either way.
            }
            // Race: another request inserted the same (workspace_id, sha256)
            // between our SELECT and INSERT. Return the canonical row.
            $canonical = DB::table('bronze.source_files')
                ->where('workspace_id', $workspaceId)
                ->where('file_sha256', $sha256)
                ->first();
            if ($canonical !== null) {
                return response()->json([
                    'duplicate' => true,
                    'source_file_id' => $canonical->id,
                    'seaweedfs_key' => $canonical->seaweedfs_key,
                ], 200);
            }
            Log::error('DrillUploadController: source_files insert failed', [
                'error' => SafeErrorMessage::forResponse($e),
                'orphaned_key_deleted' => $seaweedfsKey,
            ]);

            return response()->json(['error' => 'persist_failed'], 500);
        }

        $dispatch = $this->dispatch(
            user: $user,
            project: $project,
            workspaceId: $workspaceId,
            seaweedfsKey: $seaweedfsKey,
            selection: $selection,
            fileSize: (int) $file->getSize(),
            vendorProfileId: $vendorProfileId,
            sourceEpsg: $sourceEpsg,
        );

        $body = [
            'source_file_id' => $sourceFileId,
            'seaweedfs_key' => $seaweedfsKey,
            'sha256' => $sha256,
            'size' => $file->getSize(),
            'route' => $selection['route'],
            'sheet_type' => $selection['sheet_type'],
            'dispatch' => $dispatch,
        ];

        // Echoed only when supplied, mirroring UploadController.
        if ($sourceEpsg !== null) {
            $body['source_epsg'] = $sourceEpsg;
        }

        // A classified route (hatchet_tabular/fastapi_pdf — NOT 'unrouted',
        // which has
        // no dispatcher to fail) whose dispatch nonetheless failed used to
        // return 201 regardless, with the only signal a caller had to check
        // being `dispatch.dispatched === false` nested three levels deep.
        // The file IS stored (bronze.source_files row above), but it will
        // never be processed — 201 read as unqualified success. Surface it
        // as a real error instead of a silent dead end.
        if ($dispatch['route'] !== 'unrouted' && $dispatch['dispatched'] === false) {
            $body['error'] = 'ingestion_dispatch_failed';

            return response()->json($body, 502);
        }

        return response()->json($body, 201);
    }

    /**
     * @return array{dispatched: bool, run_id?: ?string, workflow_run_id?: ?string, sheet_type?: ?string, source_epsg?: ?int, error?: ?string, route: string}
     */
    private function dispatch(
        $user,
        Project $project,
        string $workspaceId,
        string $seaweedfsKey,
        array $selection,
        int $fileSize,
        ?int $vendorProfileId,
        ?int $sourceEpsg = null,
    ): array {
        $route = $selection['route'];

        if ($route === 'fastapi_pdf') {
            return $this->dispatchPdf(
                user: $user,
                projectId: $project->project_id,
                workspaceId: $workspaceId,
                seaweedfsKey: $seaweedfsKey,
                fileSize: $fileSize,
                vendorProfileId: $vendorProfileId,
            );
        }

        if ($route === 'hatchet_tabular') {
            return $this->dispatchTabular(
                user: $user,
                projectId: $project->project_id,
                workspaceId: $workspaceId,
                seaweedfsKey: $seaweedfsKey,
                sheetType: $selection['sheet_type'],
                sourceEpsg: $sourceEpsg,
            );
        }

        return [
            'dispatched' => false,
            'route' => 'unrouted',
            'error' => 'no_dispatcher_for_extension',
        ];
    }

    /**
     * Dispatch a CSV / XLSX drill upload to the ingest_tabular workflow.
     *
     * Mirrors UploadController::dispatchGeologyIngest() — same JWT +
     * X-Service-Key handshake, same per-workspace throttle, same
     * never-throw-out-of-the-upload-request contract. Kept as a separate
     * method rather than shared with UploadController because the two
     * surfaces derive `sheet_type` differently: the wizard has an explicit
     * category from the picker, this one has only the filename.
     *
     * `$sheetType` may be null, and that is a real state rather than a
     * failure — an unhinted CSV, or any workbook. ingest_tabular then
     * classifies from the header row (or per sheet), which is why the
     * key is omitted entirely instead of being sent as null.
     *
     * `$sourceEpsg` follows the same omit-when-null convention, and for a
     * stronger reason: IngestTabularInput treats a null source_epsg as
     * "assume DEFAULT_SOURCE_EPSG", so sending the key explicitly as null
     * says nothing the omission does not, while sending a real value is the
     * only way a caller has ever been able to correct that assumption.
     *
     * @return array{dispatched: bool, workflow_run_id?: ?string, sheet_type?: ?string, source_epsg?: ?int, error?: ?string, route: string}
     */
    private function dispatchTabular(
        User $user,
        string $projectId,
        string $workspaceId,
        string $seaweedfsKey,
        ?string $sheetType,
        ?int $sourceEpsg = null,
    ): array {
        try {
            $fastApiBase = rtrim((string) config('services.fastapi.internal_url'), '/');
            $serviceKey = config('services.fastapi.service_key');
            if (! $serviceKey) {
                Log::warning('DrillUploadController: FASTAPI_SERVICE_KEY missing — tabular ingest not dispatched');

                return ['dispatched' => false, 'route' => 'hatchet_tabular', 'error' => 'no_service_key'];
            }

            $jwt = app(FastApiJwtMinter::class)->mint(
                (string) ($user->id ?? 'unknown'),
                $projectId,
                [],
            );

            $payload = [
                'workspace_id' => $workspaceId,
                'project_id' => $projectId,
                'minio_key' => $seaweedfsKey,
                'run_id' => Str::uuid()->toString(),
            ];
            if ($sheetType !== null) {
                $payload['sheet_type'] = $sheetType;
            }
            if ($sourceEpsg !== null) {
                $payload['source_epsg'] = $sourceEpsg;
            }

            // Same per-workspace throttle as the PDF path above: an operator
            // uploading a folder of drill exports bursts exactly as hard.
            app(HatchetDispatchThrottle::class)->wait($workspaceId);

            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->retry(3, 500)->post(
                $fastApiBase.'/internal/v1/shadow/ingest_tabular/trigger',
                $payload,
            );

            if (! $resp->successful()) {
                Log::warning('DrillUploadController: tabular ingest returned non-2xx', [
                    'status' => $resp->status(),
                    'workspace_id' => $workspaceId,
                ]);

                return [
                    'dispatched' => false,
                    'route' => 'hatchet_tabular',
                    'error' => 'fastapi_'.$resp->status(),
                ];
            }

            $body = $resp->json();

            return [
                'dispatched' => true,
                'workflow_run_id' => $body['hatchet_workflow_run_id'] ?? $body['workflow_run_id'] ?? null,
                'sheet_type' => $sheetType,
                'source_epsg' => $sourceEpsg,
                'route' => 'hatchet_tabular',
            ];
        } catch (Throwable $e) {
            Log::warning('DrillUploadController: tabular dispatch failed', [
                'project_id' => $projectId,
                'error' => SafeErrorMessage::forResponse($e),
            ]);

            return ['dispatched' => false, 'route' => 'hatchet_tabular', 'error' => 'exception'];
        }
    }

    /**
     * @return array{dispatched: bool, workflow_run_id?: ?string, error?: ?string, route: string}
     */
    private function dispatchPdf(
        $user,
        string $projectId,
        string $workspaceId,
        string $seaweedfsKey,
        int $fileSize,
        ?int $vendorProfileId,
    ): array {
        try {
            $fastApiBase = rtrim(
                (string) (config('services.fastapi.internal_url')),
                '/',
            );
            $serviceKey = config('services.fastapi.service_key') ?? config('services.fastapi.service_key');
            if (! $serviceKey) {
                return ['dispatched' => false, 'route' => 'fastapi_pdf', 'error' => 'no_service_key'];
            }

            $jwt = app(FastApiJwtMinter::class)->mint(
                (string) ($user->id ?? 'unknown'),
                $projectId,
                [],
            );

            // Same per-workspace throttle as UploadController. The
            // drill-upload path can also burst (operators uploading a
            // folder of well reports), so it shares the cancellation
            // vulnerability described in [[cameco-recovery-2026-06-02]].
            app(HatchetDispatchThrottle::class)->wait($workspaceId);

            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->retry(3, 500)->post(
                $fastApiBase.'/internal/v1/shadow/ingest_pdf/trigger',
                [
                    'workspace_id' => $workspaceId,
                    'project_id' => $projectId,
                    'minio_key' => $seaweedfsKey,
                    'file_size' => $fileSize,
                    'vendor_profile_id' => $vendorProfileId,
                    'correlation_token' => 'drill-upload-'.Str::uuid()->toString(),
                ],
            );

            if (! $resp->successful()) {
                return [
                    'dispatched' => false,
                    'route' => 'fastapi_pdf',
                    'error' => 'fastapi_'.$resp->status(),
                ];
            }

            $body = $resp->json();

            return [
                'dispatched' => true,
                'workflow_run_id' => $body['hatchet_workflow_run_id'] ?? $body['workflow_run_id'] ?? null,
                'route' => 'fastapi_pdf',
            ];
        } catch (Throwable $e) {
            Log::warning('DrillUploadController: PDF dispatch failed', [
                'project_id' => $projectId,
                'error' => SafeErrorMessage::forResponse($e),
            ]);

            return ['dispatched' => false, 'route' => 'fastapi_pdf', 'error' => 'exception'];
        }
    }

    private function workspaceIdFor(string $projectId): ?string
    {
        $value = DB::table('silver.projects')
            ->where('project_id', $projectId)
            ->value('workspace_id');

        return $value === null ? null : (string) $value;
    }

    private function streamToBronze(StorageService $storage, string $key, string $localPath, ?int $vendorProfileId): void
    {
        $putOptions = [];
        if ($vendorProfileId !== null) {
            // Metadata keys must be valid C# identifiers to survive Azure
            // Blob, which answers HTTP 400 InvalidMetadata for a hyphen.
            // The old 'x-georag-vendor-profile-id' was S3-legal and would
            // have failed every upload that supplied a vendor profile.
            $putOptions['Metadata'] = [
                'vendor_profile_id' => (string) $vendorProfileId,
            ];
        }

        $handle = fopen($localPath, 'r');
        if ($handle === false) {
            throw new \RuntimeException('Unable to open uploaded file for streaming.');
        }
        try {
            $storage->bronze()->put($key, $handle, $putOptions);
        } finally {
            if (is_resource($handle)) {
                fclose($handle);
            }
        }
    }

    private function safeFilename(string $original, string $ext): string
    {
        $base = pathinfo($original, PATHINFO_FILENAME);
        $base = preg_replace('/[^A-Za-z0-9._-]+/', '_', $base) ?? 'upload';
        $base = trim($base, '._-') ?: 'upload';

        return substr($base, 0, 120).'.'.$ext;
    }
}
