<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\Request;
use Illuminate\Support\Collection;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/RasterLayersController — the project-scoped raster catalogue.
 *
 *   GET /projects/{slug}/rasters → Foundry/RasterLayers
 *
 * Why this exists
 * ---------------
 * `silver.raster_layers` had exactly one writer
 * (`src/fastapi/app/services/ingest/raster_metadata.py`, called from
 * `tiff_normalize` step 2b) and NO readers — not Laravel, not FastAPI, not
 * the frontend. A geologist uploaded a 145 MB georeferenced map sheet, it
 * ingested, a row was written, and nothing in the product ever mentioned it
 * again. One real project carried four such rows nobody could see.
 *
 * Worse for one class of file. `tiff_normalize` step 2c stops a MEASUREMENT
 * raster (a DEM, an airborne magnetics grid — CRS present, band depth wider
 * than a scanner can produce) before the PDF wrap, deliberately, because
 * running OCR over a continuous-tone surface bills for nothing and poisons
 * the recall set with character noise. That file therefore never reaches
 * `silver.reports` at all. For those rasters this table is not one surface
 * among several — it is the ONLY record that the upload succeeded.
 *
 * What this page is NOT
 * ---------------------
 * It is not a raster viewer, and the UI says so in as many words. The pixels
 * are not stored anywhere web-servable: there is no COG, no tile pyramid, no
 * PNG derivative. The original TIFF sits in bronze under the upload key and
 * is not addressable from a browser. Serving the image needs a tile path
 * that does not exist here — Martin is vector-only and is not deployed — so
 * this page renders the FOOTPRINT (the `bbox` polygon, EPSG:4326) and the
 * header facts, and states plainly that the raster is indexed but not yet
 * viewable.
 *
 * Column names, once, out loud: the primary key is `raster_id`, not
 * `raster_layer_id`, and `workspace_id` is not in the create migration —
 * it is added by `database/raw/phase0/97-rls-tenant-isolation-block2.sql` in
 * production and by `2026_05_25_184335_provision_silver_workspace_columns_for_test_db`
 * in the test DB. Both facts are load-bearing for the queries below.
 */
class RasterLayersController extends Controller
{
    use SetsWorkspaceRlsContext;

    /**
     * How many rasters the catalogue lists.
     *
     * A whole province-scale delivery is dozens of sheets, not thousands —
     * this table only ever gets a row for a file that carried a CRS. The cap
     * exists so a pathological import cannot render a 10,000-row page, and
     * the summary counts below are computed from the same capped set so the
     * numbers always describe what the reader can actually see.
     */
    private const RASTER_LIST_LIMIT = 200;

    /**
     * Bit depths a document scanner can produce.
     *
     * MIRROR of `_SCANNABLE_DTYPES` in
     * `src/fastapi/app/services/ingest/raster_metadata.py`. That module uses
     * it to decide whether `tiff_normalize` skips OCR; this controller uses
     * it to tell the reader which of their rasters were skipped and why. If
     * the Python set changes and this one does not, the page keeps rendering
     * a label that is no longer true — so change both together.
     *
     * @var list<string>
     */
    private const SCANNABLE_DTYPES = ['uint8', 'int8', 'bool', 'uint1'];

    public function index(Request $request, string $slug): Response
    {
        $project = $this->resolveProject($request, $slug);
        $workspaceId = $this->workspaceIdOrFail($project);

        $rasters = $this->rasterRows($project, $workspaceId);

        return Inertia::render('Foundry/RasterLayers', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'rasters' => $rasters->all(),
            'summary' => $this->summarise($rasters),
            // Closure: only evaluated when Inertia actually asks for it, and
            // it is a second round-trip to silver.reports that the list above
            // does not need.
            'ungeoreferenced' => fn () => $this->ungeoreferencedTiffs($project, $workspaceId),
        ]);
    }

    /**
     * Resolve the project and assert the caller is a member of it.
     *
     * Same shape as ReportController::resolveProject() — a non-member gets
     * 404 rather than 403, so probing slugs cannot distinguish "not yours"
     * from "does not exist".
     */
    private function resolveProject(Request $request, string $slug): Project
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        return $project;
    }

    /**
     * The project's workspace, or a hard stop.
     *
     * This guard is the difference between fail-closed and a cross-tenant
     * leak, and it is not theoretical. The live policy on the table is
     *
     *     USING (NULLIF(current_setting('app.workspace_id', true), '') IS NULL
     *            OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid)
     *
     * — permissive on a NULL *or empty* GUC. Handing withWorkspaceRls() an
     * empty string therefore does not scope the read to nothing; it scopes it
     * to EVERY workspace, and the page renders a healthy-looking catalogue of
     * another tenant's rasters. A project row with no workspace_id is a
     * provisioning bug, so it stops here instead.
     */
    private function workspaceIdOrFail(Project $project): string
    {
        $workspaceId = (string) ($project->workspace_id ?? '');
        if ($workspaceId === '') {
            abort(409, 'This project has no workspace assigned, so tenant-scoped data cannot be read safely.');
        }

        return $workspaceId;
    }

    /**
     * One row per indexed raster in this project.
     *
     * Two independent scopes, deliberately:
     *
     *   1. `withWorkspaceRls()` binds `app.workspace_id` for the transaction,
     *      which is what every other silver read does and what the RLS policy
     *      keys off.
     *   2. An explicit `workspace_id = ?` predicate on the query itself.
     *
     * (2) is not redundant with (1). RLS on this table is enabled but NOT
     * forced, so a connection that happens to own the table — which is
     * exactly the case in the pgsql test suite, and was the case in
     * production before the FORCE census — bypasses the policy entirely and
     * (1) filters nothing at all. The predicate is what makes the read
     * fail-closed independently of who the DB role turns out to be.
     *
     * `bbox` is a PostGIS POLYGON in 4326. ST_AsGeoJSON gives the frontend
     * something MapLibre can draw directly; the ST_XMin/… quadruple beside it
     * is what the map fits its viewport to, and computing it here beats
     * asking the browser to walk the ring.
     *
     * @return Collection<int, array<string, mixed>>
     */
    private function rasterRows(Project $project, string $workspaceId): Collection
    {
        $rows = $this->withWorkspaceRls(
            $workspaceId,
            fn () => DB::table('silver.raster_layers')
                ->where('project_id', $project->project_id)
                ->where('workspace_id', $workspaceId)
                ->orderByDesc('created_at')
                ->limit(self::RASTER_LIST_LIMIT)
                ->select(
                    'raster_id',
                    'layer_name',
                    'source_file',
                    'source_file_sha256',
                    'format',
                    'driver',
                    'width',
                    'height',
                    'band_count',
                    'crs',
                    'crs_confidence',
                    'pixel_size_x',
                    'pixel_size_y',
                    'bounds_native',
                    'compression',
                    'is_cog',
                    'has_alpha',
                    'band_stats',
                    'tags',
                    'warnings',
                    'created_at',
                    DB::raw('ST_AsGeoJSON(bbox) AS bbox_geojson'),
                    DB::raw('ST_XMin(bbox) AS bbox_west'),
                    DB::raw('ST_YMin(bbox) AS bbox_south'),
                    DB::raw('ST_XMax(bbox) AS bbox_east'),
                    DB::raw('ST_YMax(bbox) AS bbox_north'),
                    // Ground footprint. A geologist reading "4096 x 4096" has
                    // no idea whether that covers a pit bench or a province;
                    // the geography cast gives real square kilometres.
                    DB::raw('CASE WHEN bbox IS NULL THEN NULL ELSE ST_Area(bbox::geography) / 1000000.0 END AS extent_km2'),
                )
                ->get(),
        );

        return $rows->map(fn ($r) => $this->rasterListRow($r))->values();
    }

    /**
     * One entry in the catalogue.
     *
     * A named method with a declared return type rather than an inline
     * closure, for the same PHPStan reason ReportController::reportListRow()
     * documents: `Collection` is INVARIANT in TValue, so the exact array
     * shape inferred from a literal inside a closure is not a subtype of
     * `Collection<int, array<string, mixed>>` and every field added here
     * would otherwise break the caller's return type.
     *
     * @return array<string, mixed>
     */
    private function rasterListRow(object $r): array
    {
        $bandStats = $this->decodeJsonList($r->band_stats ?? null);
        $warnings = $this->decodeJsonList($r->warnings ?? null);
        $crs = isset($r->crs) && $r->crs !== '' ? (string) $r->crs : null;

        $west = $this->floatOrNull($r->bbox_west ?? null);
        $south = $this->floatOrNull($r->bbox_south ?? null);
        $east = $this->floatOrNull($r->bbox_east ?? null);
        $north = $this->floatOrNull($r->bbox_north ?? null);
        $hasBounds = $west !== null && $south !== null && $east !== null && $north !== null;

        return [
            'raster_id' => (string) ($r->raster_id ?? ''),
            'layer_name' => (string) ($r->layer_name ?? 'Unnamed layer'),
            // The file as the geologist named it. Reuses the one rule this
            // codebase has for undoing an upload-key prefix rather than
            // adding a fourth copy of it.
            'source_filename' => ReportController::filenameFromKey($r->source_file ?? null),
            'source_file' => (string) ($r->source_file ?? ''),
            'source_file_sha256' => (string) ($r->source_file_sha256 ?? ''),
            'format' => (string) ($r->format ?? ''),
            'driver' => isset($r->driver) ? (string) $r->driver : null,
            'width' => (int) ($r->width ?? 0),
            'height' => (int) ($r->height ?? 0),
            'band_count' => (int) ($r->band_count ?? 0),
            'crs' => $crs,
            'crs_confidence' => $this->floatOrNull($r->crs_confidence ?? null),
            'pixel_size_x' => $this->floatOrNull($r->pixel_size_x ?? null),
            'pixel_size_y' => $this->floatOrNull($r->pixel_size_y ?? null),
            'compression' => isset($r->compression) ? (string) $r->compression : null,
            'is_cog' => (bool) ($r->is_cog ?? false),
            'has_alpha' => (bool) ($r->has_alpha ?? false),
            'bounds_native' => $this->decodeJsonList($r->bounds_native ?? null),
            'band_stats' => $bandStats,
            'tags' => $this->decodeJsonMap($r->tags ?? null),
            'warnings' => $warnings,
            'warning_count' => count($warnings),
            // GeoJSON geometry, already decoded — the page hands it straight
            // to a MapLibre geojson source. Null when the parser could not
            // reproject the native bounds to 4326 (it emits a
            // `reprojection_failed` warning in that case, which is in
            // `warnings` above, so the UI can say WHY there is no footprint).
            'bbox' => $this->decodeJsonMap($r->bbox_geojson ?? null),
            'bounds' => $hasBounds ? [$west, $south, $east, $north] : null,
            'extent_km2' => $this->floatOrNull($r->extent_km2 ?? null),
            // The single most important fact on the row and the reason the
            // page exists: a raster with no CRS is a picture, not a map. It
            // cannot be placed, clipped, overlaid or queried spatially until
            // somebody georeferences it.
            'georeferenced' => $crs !== null,
            'ocr_skipped' => $this->isMeasurementRaster($crs, $bandStats),
            'created_at' => isset($r->created_at) ? (string) $r->created_at : null,
        ];
    }

    /**
     * True when `tiff_normalize` would have stopped this raster before OCR.
     *
     * MIRROR of `_is_measurement_raster()` in
     * `src/fastapi/app/services/ingest/raster_metadata.py`, evaluated against
     * the band statistics that same module persisted. It takes BOTH a CRS and
     * a non-scanner bit depth, exactly as the Python does, so an 8-bit
     * scanned-then-georeferenced sheet is not mislabelled.
     *
     * Why the page needs it: for these files there is no `silver.reports`
     * row, no passages and no chat retrieval — the workflow ends at step 2c.
     * Rendering them beside OCR'd map sheets with no distinction would tell
     * the reader they can ask about the contents of a magnetics grid.
     *
     * @param array<int, mixed> $bandStats
     */
    private function isMeasurementRaster(?string $crs, array $bandStats): bool
    {
        if ($crs === null) {
            return false;
        }

        $dtypes = [];
        foreach ($bandStats as $band) {
            $dtype = is_array($band) ? ($band['dtype'] ?? null) : null;
            if (is_string($dtype) && $dtype !== '') {
                $dtypes[] = strtolower($dtype);
            }
        }

        // No readable band depth means we cannot tell, and the Python is
        // conservative in the same direction: unknown goes through OCR.
        if ($dtypes === []) {
            return false;
        }

        return array_intersect($dtypes, self::SCANNABLE_DTYPES) === [];
    }

    /**
     * Catalogue-level counts for the strip above the list.
     *
     * @param Collection<int, array<string, mixed>> $rasters
     *
     * @return array<string, mixed>
     */
    private function summarise(Collection $rasters): array
    {
        return [
            'total' => $rasters->count(),
            'georeferenced' => $rasters->where('georeferenced', true)->count(),
            'missing_crs' => $rasters->where('georeferenced', false)->count(),
            // A row with a CRS but no footprint means the parser could not
            // reproject its native bounds — it is indexed but unplaceable on
            // the map, which looks identical to "not georeferenced" unless
            // it is counted separately.
            'missing_footprint' => $rasters->whereNull('bounds')->count(),
            'cloud_optimized' => $rasters->where('is_cog', true)->count(),
            'ocr_skipped' => $rasters->where('ocr_skipped', true)->count(),
            'with_warnings' => $rasters->filter(fn (array $r) => ($r['warning_count'] ?? 0) > 0)->count(),
            'list_limit' => self::RASTER_LIST_LIMIT,
            'truncated' => $rasters->count() >= self::RASTER_LIST_LIMIT,
        ];
    }

    /**
     * TIFFs that reached the PDF wrap without ever producing a raster row.
     *
     * This is the honest answer to "which of my map sheets arrived with no
     * GeoTIFF keys", and a `crs IS NULL` filter on `silver.raster_layers`
     * does NOT answer it. `persist_raster_metadata()` returns early with
     * `reason="no_crs"` and writes NOTHING when the header carries no CRS, so
     * an ungeoreferenced TIFF produces no row to flag — it is invisible to
     * any query over that table, which is precisely how "5 of 10 TIFFs on a
     * real delivery carried no CRS" stayed unreported.
     *
     * What it leaves behind is a `silver.reports` row whose
     * `source_object_key` is the derived PDF. `derived_pdf_key()` in
     * `src/fastapi/app/hatchet_workflows/tiff_normalize.py` mints that key as
     *
     *     reports/{project_id}/tiff-derived-{sha8}-{safe_stem}.pdf
     *
     * where `sha8` is the first eight hex of the ORIGINAL TIFF's sha256 — the
     * same hash `persist_raster_metadata` stores in `source_file_sha256`.
     * So "a TIFF went through the wrap and has no raster row" is exactly
     * "a tiff-derived key whose sha8 matches no `left(source_file_sha256, 8)`
     * in this project", which is what the NOT EXISTS below asks.
     *
     * Two honest limits, stated because the UI repeats them:
     *   - it cannot separate "no CRS" from "header unreadable"; both take the
     *     same early return in the writer;
     *   - a TIFF uploaded before raster capture shipped (2026-08) also lands
     *     here, because there was no writer at the time.
     *
     * @return array<int, array<string, mixed>>
     */
    private function ungeoreferencedTiffs(Project $project, string $workspaceId): array
    {
        $rows = $this->withWorkspaceRls(
            $workspaceId,
            fn () => DB::table('silver.reports AS r')
                ->where('r.project_id', $project->project_id)
                ->where('r.workspace_id', $workspaceId)
                ->where('r.source_object_key', 'like', '%tiff-derived-%')
                ->whereRaw(<<<'SQL'
                    NOT EXISTS (
                        SELECT 1
                          FROM silver.raster_layers rl
                         WHERE rl.project_id = r.project_id
                           AND left(rl.source_file_sha256, 8)
                               = substring(r.source_object_key from 'tiff-derived-([0-9a-f]{8})-')
                    )
                SQL)
                ->orderByDesc('r.created_at')
                ->limit(self::RASTER_LIST_LIMIT)
                ->select('r.report_id', 'r.title', 'r.source_object_key', 'r.created_at')
                ->get(),
        );

        return $rows->map(fn ($r) => [
            'report_id' => (string) ($r->report_id ?? ''),
            'title' => (string) ($r->title ?? 'Untitled'),
            'source_filename' => self::filenameFromDerivedTiffKey($r->source_object_key ?? null),
            'created_at' => isset($r->created_at) ? (string) $r->created_at : null,
        ])->values()->all();
    }

    /**
     * Recover the uploaded TIFF's name from the derived-PDF key.
     *
     * Mirrors `derived_pdf_key()` in `tiff_normalize.py`:
     * `tiff-derived-{sha8}-{safe_stem}.pdf`. ReportController::filenameFromKey()
     * is the wrong tool here — it strips a `{Ymd}_{His}_` upload prefix, which
     * this key does not have, so it would hand the reader the machine string
     * `tiff-derived-a1b2c3d4-Geologic_Map.pdf`.
     *
     * APPROXIMATE by construction, and the UI must not claim otherwise: the
     * Python ran the stem through `_SAFE_STEM_RE` (every character outside its
     * safe class became `_`) and truncated it to 80, so a sheet named
     * "Unga 1982b (rev 2).tif" comes back as "Unga_1982b__rev_2_". The
     * extension is not restored for the same reason — the original could have
     * been .tif, .tiff or an .rrd the workflow converted.
     */
    public static function filenameFromDerivedTiffKey(?string $key): ?string
    {
        if (! is_string($key) || $key === '') {
            return null;
        }

        $segment = str_contains($key, '/') ? substr($key, strrpos($key, '/') + 1) : $key;
        if (preg_match('/^tiff-derived-[0-9a-f]{8}-(.+)\.pdf$/i', $segment, $m) === 1) {
            return $m[1];
        }

        // Not a shape we mint. Returning the segment is still something the
        // operator can search bronze for, which "" is not.
        return $segment;
    }

    /**
     * Decode a jsonb column that should hold a list.
     *
     * The pgsql driver hands back a string; a driver or a future cast that
     * hands back an already-decoded array must not double-decode.
     *
     * @param mixed $raw
     *
     * @return array<int, mixed>
     */
    private function decodeJsonList($raw): array
    {
        $decoded = is_string($raw) ? json_decode($raw, true) : $raw;

        return is_array($decoded) ? array_values($decoded) : [];
    }

    /**
     * Decode a jsonb / ST_AsGeoJSON column that should hold an object.
     *
     * @param mixed $raw
     *
     * @return array<string, mixed>|null
     */
    private function decodeJsonMap($raw): ?array
    {
        $decoded = is_string($raw) ? json_decode($raw, true) : $raw;

        return is_array($decoded) ? $decoded : null;
    }

    /**
     * @param mixed $raw
     */
    private function floatOrNull($raw): ?float
    {
        return ($raw === null || $raw === '') ? null : (float) $raw;
    }
}
