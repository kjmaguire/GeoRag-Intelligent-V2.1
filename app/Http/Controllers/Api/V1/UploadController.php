<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Services\FastApiJwtMinter;
use App\Services\Ingestion\HatchetDispatchThrottle;
use App\Services\Ingestion\ShadowRouter;
use App\Services\StorageService;
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
 * File upload controller — uploads exploration data files to MinIO bronze bucket.
 *
 * Files land in the georag-bronze bucket under a category/project prefix.
 * PDF reports are dispatched directly to Hatchet's ingest_pdf workflow;
 * TIFF reports go through tiff_normalize first. Both triggers happen in the
 * upload request after the bronze manifest row is written.
 */
class UploadController extends Controller
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

    /**
     * Ceiling for `source_crs_wkt`, in CHARACTERS — a string rule, not a
     * file rule. Named for the same reason as the EPSG bounds above: the
     * size-cap guard greps this file for literal `'max:<5+ digits>'` and a
     * character ceiling should not have to weaken it. Mirrors the
     * max_length on IngestSpatialInput.source_crs_wkt; real `.prj` files
     * are under 4 KB, WKT2 with axis metadata can run long, and 64 KiB is
     * far above both while still refusing a pasted novel.
     */
    private const SOURCE_CRS_WKT_MAX_CHARS = 65536;

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
    /**
     * `reports` extensions that are RASTERS, not PDFs.
     *
     * One definition because three places need the same answer — the storage
     * prefix, the dispatch target, and the category list — and they were three
     * separate `in_array($ext, ['tif', 'tiff'])` literals. Adding a format to
     * two of the three routes the upload to the wrong workflow, or files it
     * under `reports/` where the PDF sensor picks it up and fails on bytes
     * that are not a PDF.
     *
     * @var list<string>
     */
    private const RASTER_REPORT_EXTS = ['tif', 'tiff', 'rrd'];

    private const CATEGORIES = [
        // ADR-0005 (2026-05-23): TIFF scans normalize to PDF at the bronze
        // edge via tiff_normalize, then route through the §04p PDF stack
        // unchanged. Both extensions land under the same `reports/{project_id}/...`
        // prefix; dispatchShadowIfPdf() inspects the extension and calls
        // the right trigger endpoint.
        // 'rrd' added 2026-08-25 — an ERDAS reduced-resolution pyramid.
        // Normally a derived companion of a raster and safely ignorable, but
        // NOT when its parent is absent: in a real delivery both .rrd files
        // held the only surviving copy of their image (a 1504x2007 colour
        // geological map and an underground mine plan). tiff_normalize
        // extracts the finest level and the rest of the raster path runs
        // unchanged.
        'reports' => ['pdf', 'tif', 'tiff', 'rrd'],
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
        // Standalone dBASE tables → ingest_tabular. Added 2026-08-23; before
        // it, '.dbf' was in no live category and a shapefile's attribute
        // table delivered without its .shp could not be uploaded at all.
        //
        // Its own key rather than a slot in `excel` or in the four drill
        // categories, for reasons that are all about the sheet_type hint:
        // nothing in a .dbf's extension says which drill table it holds, so
        // listing it under collars/surveys/lithology/samples would make the
        // picker's categoryForExtension() choose one arbitrarily and pin a
        // wrong hint on every auto-routed file — the same mistake the
        // sheet_type comment in dispatchGeologyIngest() calls out for
        // workbooks. `excel` would carry the right (absent) hint but the
        // wrong label; a dBASE table is not a workbook, and the label is
        // what the geologist reads at the drop zone.
        //
        // A .dbf that sits BESIDE a same-stem .shp is a shapefile sidecar,
        // not an entry here: groupShapefiles() zips it into the bundle
        // before upload. The two cases are discriminated by that sibling,
        // never by the extension alone.
        // `.dat` joins `.dbf`: a MapInfo attribute half IS a dBASE file and
        // reads standalone when its master is absent. Adding it here collides
        // with nothing — RETIRED_CATEGORIES is consulted by category NAME
        // (array_key_exists on the requested category), never by extension, so
        // the retired `xyz` entry for .dat has never gated anything. `txt` has
        // sat in both retired `xyz` and live `collars` for months and uploads
        // fine, which is the standing proof.
        // 'mdb'/'accdb' added 2026-08-25 — Microsoft Access. Read via
        // mdbtools in the fastapi runtime image; one Access table becomes one
        // attribute_tables layer, so a 19-table survey database lands as 19
        // named tables rather than one opaque blob.
        'tables' => ['dbf', 'dat', 'mdb', 'accdb'],
        // Vector data + QGIS projects → ingest_spatial →
        // silver.spatial_features. `.zip` is here because a shapefile is
        // never one file: .shp/.shx/.dbf/.prj travel together, and a lone
        // .shp cannot be read without its siblings.
        // 'gdb' and 'dgn' added 2026-08-21. Both were already in
        // ingest_spatial.VECTOR_EXTENSIONS and both are read by the parser;
        // the architecture doc lists File Geodatabase as In-V1. They were
        // simply missing from the accepted list, so an ArcGIS shop's
        // standard delivery format 422'd at the door.
        //
        // 'tab' and 'mif' added 2026-08-23 — MapInfo, which GDAL reads via
        // the "MapInfo File" driver present in the deployed image. Only the
        // two ENTRY POINTS are listed. MapInfo's sidecars (.dat/.map/.id/
        // .ind for TAB, .mid for MIF) are deliberately absent: a .mid opens
        // directly as a dataset, so accepting it as its own upload would
        // ingest a MIF/MID pair twice, and .map/.id/.ind carry geometry or an
        // index and mean nothing without their master. '.dat' is NOT in that
        // group — it is the attribute half, a whole dBASE table, and it lives
        // in `tables` above. (The comment that used to sit here said .dat was
        // "already claimed by the retired xyz category"; RETIRED_CATEGORIES is
        // consulted by category NAME, never by extension, so that constraint
        // never existed.) Sidecars reach the parser inside the bundle `zip`,
        // exactly as a shapefile's do.
        //
        // 'str' added 2026-08-25 — Surpac string files (mine-design strings:
        // vein outlines, level plans). No OGR driver exists, so spatial_parser
        // returns early to a hand-written reader. Like a .dxf it declares no
        // coordinate system and needs an EPSG at upload time.
        'spatial' => [
            'geojson', 'json', 'shp', 'gpkg', 'gml', 'gpx', 'dxf', 'dgn',
            'fgb', 'gdb', 'zip', 'qgs', 'qgz', 'tab', 'mif', 'str',
        ],
        // LAS downhole curves -> ingest_well_logs -> silver.well_log_curves.
        // One row per CURVE with depth/value arrays, not a row per sample:
        // a 3,000 m hole logged every 15 cm is 20,000 samples per curve.
        'well_logs' => ['las'],
    ];

    /**
     * Every bronze path prefix an upload can land under.
     *
     * This is CATEGORIES' keys plus the two prefixes that are not category
     * names:
     *
     *   tiff     — ADR-0005 routes TIFFs to their own prefix while keeping
     *              the `reports` category for UX (see storeFile()).
     *   tabular  — written by ingest_zip_archive when it re-uploads a CSV
     *              or workbook found inside an archive so ingest_tabular can
     *              classify it. No user ever picks this category; the files
     *              still need to show up on the Ingestion Runs page.
     *
     * Exposed because IngestionRunsController scans bronze directly as a
     * fallback for uploads whose progress row has not appeared yet, and a
     * hand-maintained second copy of this list is exactly how CSV, XLSX,
     * shapefile, GeoPackage and LAS uploads became invisible on that page.
     *
     * @return list<string>
     */
    public static function bronzePrefixes(): array
    {
        return [...array_keys(self::CATEGORIES), 'tiff', 'tabular'];
    }

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
     *   file        — the file (required, max 100 MB)
     *   category    — one of the CATEGORIES keys (required)
     *   source_epsg — optional EPSG integer (1024-32767) asserting the CRS of
     *                 a file that declares none. Forwarded to ingest_spatial
     *                 and ingest_tabular; ignored by every other workflow.
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
            // Derived from the same ceiling Swoole's package_max_length uses.
            // This rule used to read `max:6291456` — 6 GiB, in kilobytes,
            // annotated "6 GB" — which the transport would never allow
            // through: Swoole refused the connection at 2 GiB, so an
            // oversized upload got a dropped socket instead of this 422.
            'file' => ['required', 'file', 'max:'.Uploads::maxKilobytes()],
            'category' => ['required', 'string', 'in:'.implode(',', array_keys(self::CATEGORIES))],
            'vendor_profile_id' => ['nullable', 'integer', 'exists:vendor_profiles,id'],
            // Operator-supplied CRS for a file that declares none — a
            // shapefile shipped without its .prj, a .dbf of eastings and
            // northings. It is an EPSG *integer*, never a 'EPSG:26904'
            // string: the same rule as StoreQueryRequest's
            // context_envelope.crs_epsg, matching the DB CHECK
            // (crs_epsg_native BETWEEN 1024 AND 32767) on the column it
            // eventually lands in. The parser applies it ONLY when the file
            // declares no CRS of its own — a declared CRS always wins.
            'source_epsg' => ['nullable', 'integer', 'min:'.self::EPSG_MIN, 'max:'.self::EPSG_MAX],
            // The `.prj` text the wizard's CRS donation found in the same
            // drop, for a spatial file that cannot carry a `.prj` member of
            // its own (a lone .dxf/.dgn is one file, not a ZIP the copy
            // could be zipped into). Raw WKT, resolved to an EPSG integer
            // server-side by ingest_spatial via pyproj — the browser
            // deliberately does no WKT→EPSG of its own (shapefileBundle.ts,
            // crsLabel). Ignored by the workflow whenever source_epsg is
            // also present: a typed code outranks a found copy.
            //
            // Concatenated, not a literal: UploadSizeCapConsistencyTest
            // forbids any literal five-plus-digit `max:` in this file. That
            // trap exists for FILE rules, where Laravel measures `max` in
            // kilobytes — this is a string rule measured in characters, but
            // the regex cannot tell, and the named constant reads better.
            'source_crs_wkt' => ['nullable', 'string', 'max:'.self::SOURCE_CRS_WKT_MAX_CHARS],
        ], [
            // store() validates inline rather than through a FormRequest, so
            // there is no messages() to hang these on. Without them an
            // out-of-range code answers Laravel's default "The source epsg
            // field must be at least 1024." instead of the wording the rest
            // of the platform already uses (StoreQueryRequest::messages()).
            'source_epsg.min' => 'EPSG codes must be in the range 1024-32767.',
            'source_epsg.max' => 'EPSG codes must be in the range 1024-32767.',
        ]);

        $file = $request->file('file');
        $category = $validated['category'];
        $vendorProfileId = $validated['vendor_profile_id'] ?? null;
        $sourceEpsg = $validated['source_epsg'] ?? null;
        $sourceCrsWkt = $validated['source_crs_wkt'] ?? null;

        // Validate file extension against category
        $ext = strtolower($file->getClientOriginalExtension());
        $allowedExts = self::CATEGORIES[$category];
        if (! in_array($ext, $allowedExts, true)) {
            return response()->json([
                'message' => "Invalid file extension '.{$ext}' for category '{$category}'. Allowed: ".implode(', ', $allowedExts),
            ], 422);
        }

        // ── Content checks ───────────────────────────────────────────────
        // Until 2026-08-22 the extension above was the ONLY input to both
        // acceptance and routing, and it is client-supplied. A zip bomb
        // renamed `report.pdf` was stored under reports/ and dispatched to
        // ingest_pdf, which spent worker memory failing on it.
        //
        // finfo reads magic bytes only, so this is cheap even on a
        // multi-GB upload — and it is deliberately lenient: it rejects a
        // clear contradiction (ZIP magic under a .pdf name) and stays
        // silent on everything ambiguous. Most geological formats have no
        // usable signature; see UploadContentGuard for why that asymmetry
        // is the right way round.
        try {
            $sniffed = $file->getMimeType();
        } catch (Throwable) {
            $sniffed = null;
        }
        if (UploadContentGuard::mimeMismatch($ext, $sniffed)) {
            return response()->json([
                'error' => 'content_type_mismatch',
                'message' => "This file is named '.{$ext}' but its contents are "
                    ."'{$sniffed}'. Rename it to match, or upload it under the "
                    .'category that accepts that format.',
            ], 422);
        }

        // A ZIP is opened before it is stored. Both extractors bound entry
        // count and expanded size, but they run AFTER the object is written
        // and a workflow dispatched — so without this an archive bomb still
        // costs storage, an ingest_progress row and a failed run.
        if ($ext === 'zip') {
            $archiveProblem = UploadContentGuard::rejectArchive($file->getRealPath());
            if ($archiveProblem !== null) {
                return response()->json([
                    'error' => 'unusable_archive',
                    'message' => $archiveProblem,
                ], 422);
            }
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
        if ($category === 'reports' && in_array($ext, self::RASTER_REPORT_EXTS, true)) {
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
                // Metadata keys must be valid C# identifiers to survive Azure
                // Blob, which answers HTTP 400 InvalidMetadata for a hyphen.
                // The old 'x-georag-vendor-profile-id' was S3-legal and would
                // have failed every upload that supplied a vendor profile.
                $putOptions['Metadata'] = [
                    'vendor_profile_id' => (string) $vendorProfileId,
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
                'source_epsg' => $sourceEpsg,
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

            // Echoed back so the caller can see the override was understood.
            // Absent when none was supplied, same as vendor_profile_id.
            if ($sourceEpsg !== null) {
                $responseData['source_epsg'] = $sourceEpsg;
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
                    isTiff: in_array($ext, self::RASTER_REPORT_EXTS, true),
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
                    sourceEpsg: $sourceEpsg,
                    // CAD formats only — the two with no CRS concept and no
                    // sidecar GDAL would read, which is the entire reason
                    // the donation has to travel as text. For every other
                    // spatial format a WKT here can only mislead: the
                    // parser would ignore it for a CRS-declaring file while
                    // the workflow had already trusted it, and the wizard
                    // never sends it for those anyway.
                    sourceCrsWkt: in_array($ext, ['dxf', 'dgn'], true)
                        ? $sourceCrsWkt
                        : null,
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
                config('services.fastapi.internal_url'),
                '/',
            );
            $serviceKey = config('services.fastapi.service_key');
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
     * Re-run ingest_tabular against an object already in bronze, with a
     * column mapping the user confirmed.
     *
     * Public because IngestionRunsController owns the surface a geologist
     * corrects a mapping from, and this controller owns the one place that
     * knows how to reach the Hatchet trigger — service key, per-user JWT
     * and the per-workspace dispatch throttle. A second copy of that in the
     * Foundry controller is exactly the duplication that has already cost
     * this codebase three drifting alias lists.
     *
     * Unlike the upload-time dispatchers, failures are RETURNED rather than
     * swallowed. Those run inside an upload whose response must not be
     * blocked by a trigger problem; this one has no other purpose, so a
     * silent failure would leave the user watching a run that was never
     * started.
     *
     * @param array<string, array<string, string>> $columnMap
     *
     * @return array<string, mixed>
     */
    public function dispatchTabularRemap(
        string $userId,
        string $workspaceId,
        string $projectId,
        string $minioKey,
        string $sheetType,
        array $columnMap,
    ): array {
        $fastApiBase = rtrim((string) config('services.fastapi.internal_url'), '/');
        $serviceKey = config('services.fastapi.service_key');
        if (! $serviceKey) {
            Log::warning('UploadController: FASTAPI_SERVICE_KEY missing — remap not dispatched');

            return ['dispatched' => false, 'error' => 'no_service_key'];
        }

        $payload = [
            'workspace_id' => $workspaceId,
            'project_id' => $projectId,
            'minio_key' => $minioKey,
            'run_id' => Str::uuid()->toString(),
            'sheet_type' => $sheetType,
            'column_map' => $columnMap,
        ];

        try {
            // The identifier, not the user object: minting a JWT is all this
            // needs, and taking a whole model to read one field invites a
            // caller to believe more of it is used than is.
            $jwt = app(FastApiJwtMinter::class)->mint($userId, $projectId, []);

            $this->dispatchThrottle->wait($workspaceId);

            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(15)->retry(3, 500)->post(
                $fastApiBase.'/internal/v1/shadow/ingest_tabular/trigger',
                $payload,
            );
        } catch (Throwable $e) {
            Log::warning('UploadController: remap dispatch threw', [
                'project_id' => $projectId,
                'error' => $e->getMessage(),
            ]);

            return ['dispatched' => false, 'error' => 'dispatch_failed'];
        }

        if (! $resp->successful()) {
            Log::warning('UploadController: remap trigger returned non-2xx', [
                'status' => $resp->status(),
                'project_id' => $projectId,
            ]);

            return ['dispatched' => false, 'error' => 'fastapi_'.$resp->status()];
        }

        $body = $resp->json();

        Log::info('UploadController: tabular remap dispatched', [
            'project_id' => $projectId,
            'minio_key' => $minioKey,
            'sheet_type' => $sheetType,
            // The FIELD names only. The column names are the user's own
            // spreadsheet headers and can carry anything, including data.
            'mapped_fields' => array_keys($columnMap[$sheetType] ?? []),
        ]);

        return [
            'dispatched' => true,
            'run_id' => $payload['run_id'],
            'workflow_run_id' => $body['hatchet_workflow_run_id']
                ?? $body['workflow_run_id'] ?? null,
        ];
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
                config('services.fastapi.internal_url'),
                '/',
            );
            $serviceKey = config('services.fastapi.service_key');
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
        // Without this line the `tables` category above would answer 201,
        // write the object, and dispatch nothing — the retired-category bug
        // this docblock describes, reproduced by a one-line omission.
        'tables' => 'ingest_tabular',
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
     *
     * (No `@param array<string, mixed> $responseData` here on purpose:
     * phpstan-baseline.neon carries the missingType.iterableValue entry for
     * this parameter, and typing it turns that entry into a non-ignorable
     * `ignore.unmatched` error. Removing the baseline line and the docblock
     * belong in the same commit; that file is outside this change.)
     *
     * @param int|null $sourceEpsg Operator-supplied CRS for a file that
     *                             declares none. Forwarded to ingest_spatial
     *                             and ingest_tabular, which both accept
     *                             `source_epsg` on their input model, and
     *                             withheld from ingest_well_logs, which does
     *                             not.
     * @param string|null $sourceCrsWkt Donated `.prj` text for a spatial
     *                                  file that cannot carry the copy as a
     *                                  ZIP member. Forwarded to
     *                                  ingest_spatial only — the only input
     *                                  model that declares it — and only
     *                                  when no source_epsg was typed, since
     *                                  the workflow ignores it then anyway.
     */
    private function dispatchGeologyIngest(
        $user,
        string $category,
        string $projectId,
        string $minioKey,
        array &$responseData,
        ?int $sourceEpsg = null,
        ?string $sourceCrsWkt = null,
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
                config('services.fastapi.internal_url'),
                '/',
            );
            $serviceKey = config('services.fastapi.service_key');
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
            //
            // Two categories deliberately produce no hint. A workbook holds
            // several tables, so pinning one type would make ingest_tabular
            // treat every sheet as that type instead of classifying each on
            // its own; and a `.dbf`'s extension says nothing about which
            // drill table it holds. Both are better served by the header-row
            // classifier.
            //
            // The key is omitted rather than sent as null: ingest_tabular
            // reads a missing sheet_type as "classify this", so a null adds
            // nothing and invites a later reader to treat it as a decision.
            // This used to be written as `$category !== 'excel'`, which sent
            // an explicit null the moment a second hint-less category
            // existed — deriving the omission from the match's own result
            // removes that trap instead of adding a name to it.
            $sheetType = $workflow === 'ingest_tabular'
                ? match ($category) {
                    'collars' => 'collar',
                    'surveys' => 'survey',
                    'lithology' => 'lithology',
                    'samples' => 'sample',
                    default => null,
                }
            : null;
            if ($sheetType !== null) {
                $payload['sheet_type'] = $sheetType;
            }

            // The CRS override, when the operator supplied one. Sent only to
            // the two workflows whose input model declares `source_epsg`;
            // ingest_well_logs has no such field and no coordinates to place.
            //
            // Until this existed, ingest_tabular had NEVER been sent a
            // source_epsg from anywhere, so every drill CSV the platform has
            // ingested silently assumed its DEFAULT_SOURCE_EPSG of 32613
            // (UTM 13N) — correct in Saskatchewan, a continent out in Alaska.
            if ($sourceEpsg !== null
                && in_array($workflow, ['ingest_tabular', 'ingest_spatial'], true)
            ) {
                $payload['source_epsg'] = $sourceEpsg;
            }

            // The donated `.prj` text, for ingest_spatial only — the one
            // input model that declares `source_crs_wkt` — and only when no
            // EPSG integer was typed: the workflow prefers the typed code
            // regardless, so sending both would be dead weight in every log
            // line that quotes the payload.
            if ($sourceCrsWkt !== null
                && $sourceEpsg === null
                && $workflow === 'ingest_spatial'
            ) {
                $payload['source_crs_wkt'] = $sourceCrsWkt;
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
