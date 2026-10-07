<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Support\HoleId;
use App\Support\HoleStripTracks;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Collection;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/WorkspaceController — project workspace with 5-mode switcher
 * (MAP / SECTION / 3D / STRUCTURE / LOGS).
 *
 * 2026-08-17 — restored after the 2026-07-27 reader-core trim. The original
 * never bound app.workspace_id before its ~24 independently try/catch'd
 * query blocks — added SetsWorkspaceRlsContext on restore, same as
 * DrillholeDetailController/HoleCompareController. silver.saved_map_views
 * (queried for savedViewsCount below) is now fail-closed RLS
 * (second RLS pass, 2026-08-15) — without this wrap that count silently
 * stayed 0 for every project, which is exactly the kind of self-inflicted
 * regression this retrofit exists to avoid.
 *
 * The whole method body is wrapped in ONE withWorkspaceRls() closure rather
 * than one per try/catch block: withWorkspaceRls() only sets the GUC inside
 * a DB transaction.
 *
 * *Corrected 2026-09-29 (LAR-4):* this used to say each inner try/catch was
 * enough to keep the panels independent. On Postgres it was not — the first
 * failed statement aborts the shared transaction (25P02), so every panel after
 * it failed too and rendered empty. Each optional block now opens its own
 * savepoint (openSavepoint / releaseSavepoint / rollBackToSavepoint from
 * SetsWorkspaceRlsContext), and a failure rolls back only that block.
 */
class WorkspaceController extends Controller
{
    use SetsWorkspaceRlsContext;

    /**
     * Payload bounds. One collar set is the source of truth for every
     * per-hole panel on the page (map, 3D intervals, surveys, assays...) so
     * the caps nest instead of each panel choosing its own hole population:
     * the interval holes are a PREFIX of the returned collars, and surveys are
     * fetched for exactly the returned collars. Every cap that bites is
     * reported in the `truncation` prop so the UI can say so.
     */
    private const MAX_WORKSPACE_COLLARS = 1000;

    /**
     * True for a collar whose terrain lookup was made at its present position
     * (silver.collars.elevation_dem_geom still equals geom_4326). A collar that
     * has moved since keeps an old elevation_dem_m until the next promotion
     * looks it up again; that value must not be served meanwhile.
     */
    private const TERRAIN_LOOKUP_CURRENT = 'elevation_dem_geom IS NOT NULL AND ST_Equals(elevation_dem_geom, geom_4326)';

    private const MAX_INTERVAL_HOLES = 200;

    private const MAX_INTERVAL_BANDS_PER_HOLE = 80;

    private const MAX_SURVEY_STATIONS_PER_HOLE = 100;

    /** Curves drawn when the user has not picked any (gamma family first). */
    private const DEFAULT_LOG_TRACKS = 8;

    /** Hard ceiling on tracks in one payload, whatever ?log_curves= asks for. */
    private const MAX_LOG_TRACKS = 12;

    /** Points per curve after downsampling (SVG perf + payload size). */
    private const LOG_CURVE_TARGET_POINTS = 240;

    /**
     * Small, data-driven alias map. Used ONLY for ordering, colour and a
     * fallback unit label — a curve whose name is not listed is still listed,
     * selectable and plotted (group `other`), never dropped.
     *
     * @var array<string, array{label: string, color: string, unit: string|null, aliases: list<string>}>
     */
    private const LOG_CURVE_GROUPS = [
        'gamma' => ['label' => 'Gamma', 'color' => 'oklch(0.78 0.16 30)', 'unit' => 'cps', 'aliases' => ['GAMMA', 'GR', 'GAM', 'GRD', 'GAMMA_RAY']],
        'grade' => ['label' => 'U grade', 'color' => 'oklch(0.82 0.18 145)', 'unit' => '%eU₃O₈', 'aliases' => ['GRADE']],
        'resistivity' => ['label' => 'Resistivity', 'color' => 'oklch(0.72 0.14 220)', 'unit' => 'Ω·m', 'aliases' => ['RES', 'RESIST', 'RT', 'RESISTIVITY']],
        'sp' => ['label' => 'SP', 'color' => 'oklch(0.65 0.10 280)', 'unit' => 'mV', 'aliases' => ['SP']],
        'ip' => ['label' => 'IP', 'color' => 'oklch(0.75 0.15 330)', 'unit' => null, 'aliases' => ['IP', 'CHARGEABILITY']],
        'susceptibility' => ['label' => 'Susceptibility', 'color' => 'oklch(0.72 0.14 180)', 'unit' => null, 'aliases' => ['SUSC', 'MAGSUS']],
        'density' => ['label' => 'Density', 'color' => 'oklch(0.70 0.12 100)', 'unit' => null, 'aliases' => ['DEN', 'DENS', 'RHOB']],
        'caliper' => ['label' => 'Caliper', 'color' => 'oklch(0.68 0.08 60)', 'unit' => null, 'aliases' => ['CAL', 'CALI', 'CALIPER']],
    ];

    /** Colours for curves outside every alias group (picked by name hash). */
    private const LOG_CURVE_FALLBACK_COLORS = [
        'oklch(0.74 0.13 15)', 'oklch(0.74 0.13 55)', 'oklch(0.74 0.13 120)',
        'oklch(0.74 0.13 165)', 'oklch(0.74 0.13 200)', 'oklch(0.74 0.13 250)',
        'oklch(0.74 0.13 300)', 'oklch(0.74 0.13 345)',
    ];

    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $workspaceId = (string) $project->workspace_id;

        return $this->withWorkspaceRls($workspaceId, function () use ($request, $project, $workspaceId) {
            // Deterministic order (hole_id, then collar_id as a tiebreaker) so
            // the cap always keeps the same holes and the 3D interval set below
            // is a stable prefix of this one.
            $collars = DB::table('silver.collars')
                ->where('project_id', $project->project_id)
                // CC-01 Item 2 — surface spatial uncertainty + CRS provenance
                // so WorkspaceMap can render the uncertainty-rings layer per row.
                // Orientation triple (azimuth/dip/elevation) + hole_type/status
                // feed the 3D Trajectories sub-view (MultiHole3DTrace).
                // `elevation` falls back to the terrain model's ground height
                // when the file had none (silver.collars.elevation_dem_m,
                // written by promote_silver_to_gold) so a hole is not drawn
                // at sea level; `elevation_from_terrain` says which it is.
                // Only a lookup made at the collar's present position counts:
                // one that predates a move is stale and not served.
                ->selectRaw('collar_id, hole_id, hole_id_canonical, easting, northing, total_depth, ST_X(geom_4326) AS lng, ST_Y(geom_4326) AS lat, spatial_uncertainty_m, crs_confidence, georef_method, azimuth, dip, COALESCE(elevation, CASE WHEN '.self::TERRAIN_LOOKUP_CURRENT.' THEN elevation_dem_m END) AS elevation, (elevation IS NULL AND elevation_dem_m IS NOT NULL AND '.self::TERRAIN_LOOKUP_CURRENT.') AS elevation_from_terrain, elevation_dem_source, hole_type, status')
                ->orderBy('hole_id')
                ->orderBy('collar_id')
                ->limit(self::MAX_WORKSPACE_COLLARS)
                ->get();

            $collarsTotal = $collars->count();
            if ($collarsTotal >= self::MAX_WORKSPACE_COLLARS) {
                $sp = $this->openSavepoint();
                try {
                    $collarsTotal = (int) DB::table('silver.collars')
                        ->where('project_id', $project->project_id)
                        ->count();
                    $this->releaseSavepoint($sp);
                } catch (\Throwable $e) { /* fall back to the returned count */
                    $this->rollBackToSavepoint($sp);
                }
            }

            // Project summary aggregates — drives the map-overlay header and the
            // bottom stats chip. Computed across the project's collars + derived
            // ore-band rows; cheap (one aggregate query each).
            $totalDrilledM = 0.0;
            $meanTd = null;
            $sp = $this->openSavepoint();
            try {
                $td = DB::table('silver.collars')
                    ->where('project_id', $project->project_id)
                    ->selectRaw('COALESCE(SUM(total_depth), 0) AS sum_m, AVG(total_depth) AS avg_m')
                    ->first();
                $totalDrilledM = (float) ($td->sum_m ?? 0);
                $meanTd = $td->avg_m !== null ? (float) $td->avg_m : null;
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }

            $totalOreThicknessM = 0.0;
            $oreHoleCount = 0;
            $meanU3o8Pct = null;
            $sp = $this->openSavepoint();
            try {
                $ore = DB::table('gold.drillhole_intervals_visual')
                    ->where('project_id', $project->project_id)
                    ->where('lithology_code', 'DERIVED-ORE')
                    ->selectRaw('COALESCE(SUM(depth_to - depth_from), 0) AS sum_m, COUNT(DISTINCT collar_id) AS holes')
                    ->first();
                $totalOreThicknessM = (float) ($ore->sum_m ?? 0);
                $oreHoleCount = (int) ($ore->holes ?? 0);
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }

            $sp = $this->openSavepoint();
            try {
                $meanRow = DB::table('silver.samples as s')
                    ->join('silver.collars as c', 's.collar_id', '=', 'c.collar_id')
                    ->where('c.project_id', $project->project_id)
                    ->where('s.sample_type', 'derived_composite')
                    ->selectRaw("AVG(NULLIF((s.commodity_assays->>'U3O8_pct_e')::numeric, 0)) AS mean_grade")
                    ->first();
                if ($meanRow && $meanRow->mean_grade !== null) {
                    $meanU3o8Pct = (float) $meanRow->mean_grade;
                }
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }

            // Project AOI — convex hull of all collar geometries, as GeoJSON.
            // Drives the "Project AOI" toggle on the map (dashed outline).
            $projectAoi = null;
            $sp = $this->openSavepoint();
            try {
                $hullRow = DB::table('silver.collars')
                    ->where('project_id', $project->project_id)
                    ->whereNotNull('geom_4326')
                    ->selectRaw('ST_AsGeoJSON(ST_ConvexHull(ST_Collect(geom_4326))) AS hull')
                    ->first();
                if ($hullRow && $hullRow->hull) {
                    $projectAoi = json_decode((string) $hullRow->hull, true);
                }
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }

            // Ore-band counts per collar — drives the marker styling on the
            // MapLibre layer (mineralised holes are surfaced with a brighter
            // halo). One quick aggregate query keyed by collar_id.
            $oreBandsByCollar = [];
            $sp = $this->openSavepoint();
            try {
                $oreRows = DB::table('gold.drillhole_intervals_visual')
                    ->where('project_id', $project->project_id)
                    ->where('lithology_code', 'DERIVED-ORE')
                    ->select('collar_id', DB::raw('COUNT(*) AS n'), DB::raw('SUM(depth_to - depth_from) AS thickness_m'))
                    ->groupBy('collar_id')
                    ->get();
                foreach ($oreRows as $r) {
                    $oreBandsByCollar[(string) $r->collar_id] = [
                        'count' => (int) $r->n,
                        'thickness_m' => round((float) $r->thickness_m, 2),
                    ];
                }
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback empty */
                $this->rollBackToSavepoint($sp);
            }

            $sectionsCount = 0;
            $sp = $this->openSavepoint();
            try {
                $sectionsCount = (int) DB::table('gold.cross_section_panels')
                    ->where('project_id', $project->project_id)->count();
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* may not exist */
                $this->rollBackToSavepoint($sp);
            }

            $intervalsCount = 0;
            $sp = $this->openSavepoint();
            try {
                $intervalsCount = (int) DB::table('gold.drillhole_intervals_visual')
                    ->where('project_id', $project->project_id)->count();
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* may not exist */
                $this->rollBackToSavepoint($sp);
            }

            $structuresVisualCount = 0;
            $sp = $this->openSavepoint();
            try {
                $structuresVisualCount = (int) DB::table('gold.structure_measurements_visual')
                    ->where('project_id', $project->project_id)->count();
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* may not exist */
                $this->rollBackToSavepoint($sp);
            }

            // Raw silver-tier structures (joined via collars). Empty for Wyoming
            // today — Cameco binary `.log` parse phase hasn't extracted measured
            // structures yet. But the AZIMUTH/SANG downhole survey curves on
            // well_log_curves *do* carry deviation angles we can surface as a
            // proxy for orientation context.
            $structuresCount = 0;
            $sp = $this->openSavepoint();
            try {
                $structuresCount = (int) DB::table('silver.structure as st')
                    ->join('silver.collars as c', 'st.collar_id', '=', 'c.collar_id')
                    ->where('c.project_id', $project->project_id)
                    ->count();
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }

            // Curve type summary — drives the LOGS mode legend.
            $curveSummary = collect();
            $sp = $this->openSavepoint();
            try {
                $curveSummary = DB::table('silver.well_log_curves as wc')
                    ->join('silver.collars as c', 'wc.collar_id', '=', 'c.collar_id')
                    ->where('c.project_id', $project->project_id)
                    ->select('wc.curve_name', DB::raw('COUNT(*) as curves'), DB::raw('AVG(wc.sample_count) as avg_samples'))
                    ->groupBy('wc.curve_name')
                    ->orderByDesc('curves')
                    ->limit(20)
                    ->get();
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }
            $wellLogCurvesCount = $curveSummary->sum('curves');

            // Collars that have at least one well-log curve of ANY name — this
            // is what the LOGS hole picker shows. (It used to require a curve
            // named exactly GAMMA, which hid every hole logged with GR, RESIST,
            // IP, SUSC, DEN, CAL ... from the picker.) DISTINCT because the
            // join yields one row per curve. Ordered by hole_id so the
            // dropdown is predictable.
            $logHoleOptions = [];
            $sp = $this->openSavepoint();
            try {
                $logHoleOptions = DB::table('silver.well_log_curves as wc')
                    ->join('silver.collars as c', 'wc.collar_id', '=', 'c.collar_id')
                    ->where('c.project_id', $project->project_id)
                    ->distinct()
                    ->select('c.collar_id', 'c.hole_id', 'c.hole_id_canonical')
                    ->orderBy('c.hole_id_canonical')
                    ->orderBy('c.hole_id')
                    ->orderBy('c.collar_id')
                    ->get()
                    ->map(fn ($r) => [
                        'collar_id' => (string) $r->collar_id,
                        'hole_id' => (string) ($r->hole_id_canonical ?? $r->hole_id),
                    ])
                    ->values()
                    ->all();
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }

            // A hole whose geology was logged but which has no LAS curves used
            // to be unreachable here: the picker listed curve holes only, and
            // the panel below drew nothing without curves - so a lithology,
            // alteration or mineralization log ingested for a hole never
            // showed as a strip log. Holes with any of those are listed too.
            $sp = $this->openSavepoint();
            try {
                $withIntervals = array_flip((new HoleStripTracks)->collarsWithIntervals(
                    array_map('strval', $collars->pluck('collar_id')->all()),
                ));
                $listed = array_column($logHoleOptions, 'collar_id');
                $added = false;
                foreach ($collars as $c) {
                    $cid = (string) $c->collar_id;
                    if (isset($withIntervals[$cid]) && ! in_array($cid, $listed, true)) {
                        $logHoleOptions[] = [
                            'collar_id' => $cid,
                            'hole_id' => (string) ($c->hole_id_canonical ?? $c->hole_id),
                        ];
                        $added = true;
                    }
                }
                if ($added) {
                    usort($logHoleOptions, fn ($a, $b) => strcmp($a['hole_id'], $b['hole_id']));
                }
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback: curve holes only */
                $this->rollBackToSavepoint($sp);
            }

            // Pull the selected (or first) hole's curves and render them in the
            // LOGS panel. ?log_hole= overrides the default picker selection;
            // ?log_curves=A,B,C overrides which curves are drawn (default:
            // the gamma family first, then the rest, up to DEFAULT_LOG_TRACKS).
            // `log_available_curves` always lists EVERY curve the hole has so
            // the UI can offer a toggle for curves that are not drawn yet.
            $logTracks = [];
            $logAvailableCurves = [];
            $logSelectedCurves = [];
            $logHoleId = null;
            $logDepthMax = 0.0;
            $logHoleTotalDepth = null;
            $logHoleEasting = null;
            $logHoleNorthing = null;
            $logLithologyIntervals = [];
            $logAlterationIntervals = [];
            $logMineralizationIntervals = [];
            $logTracksTruncated = ['lithology' => false, 'alteration' => false, 'mineralization' => false];
            $sp = $this->openSavepoint();
            try {
                $requestedHole = $request->query('log_hole');
                $sampleCollar = null;
                if ($requestedHole && ! empty($logHoleOptions)) {
                    // Options are canonical ids (the database derives
                    // hole_id_canonical on every write, §04e 2026-09-29), so a
                    // link carrying the stored spelling ("HST-B") must match
                    // its canonical form ("HSTB") too.
                    $requestedCanonical = HoleId::canonicalize(is_string($requestedHole) ? $requestedHole : null);
                    foreach ($logHoleOptions as $opt) {
                        if ($opt['hole_id'] === $requestedHole || $opt['hole_id'] === $requestedCanonical) {
                            $sampleCollar = (object) ['collar_id' => $opt['collar_id'], 'hole_id_canonical' => $opt['hole_id'], 'hole_id' => $opt['hole_id']];
                            break;
                        }
                    }
                }
                if (! $sampleCollar && ! empty($logHoleOptions)) {
                    $first = $logHoleOptions[0];
                    $sampleCollar = (object) ['collar_id' => $first['collar_id'], 'hole_id_canonical' => $first['hole_id'], 'hole_id' => $first['hole_id']];
                }
                // Derived lithology intervals for the active hole — drives the
                // strip-log column in the LOGS panel. Reads gold.drillhole_intervals_visual
                // which is populated by the derive_intervals pipeline.
                if ($sampleCollar) {
                    // Lithology (with the attributes gold has no column for),
                    // alteration and mineralization: one reader, so the LOGS
                    // panel, the compare payload and the hole page agree.
                    $strip = (new HoleStripTracks)->forCollar((string) $sampleCollar->collar_id);
                    $logLithologyIntervals = $strip['lithology'];
                    $logAlterationIntervals = $strip['alteration'];
                    $logMineralizationIntervals = $strip['mineralization'];
                    $logTracksTruncated = $strip['truncated'];
                }
                if ($sampleCollar) {
                    $logHoleId = (string) ($sampleCollar->hole_id_canonical ?? $sampleCollar->hole_id);
                    $collarMeta = DB::table('silver.collars')
                        ->where('collar_id', $sampleCollar->collar_id)
                        ->select('total_depth', 'easting', 'northing')
                        ->first();
                    if ($collarMeta) {
                        $logHoleTotalDepth = $collarMeta->total_depth !== null ? (float) $collarMeta->total_depth : null;
                        $logHoleEasting = $collarMeta->easting !== null ? (float) $collarMeta->easting : null;
                        $logHoleNorthing = $collarMeta->northing !== null ? (float) $collarMeta->northing : null;
                    }
                    $requestedCurves = $request->query('log_curves');
                    $logAvailableCurves = $this->availableLogCurves((string) $sampleCollar->collar_id);
                    $logSelectedCurves = $this->selectLogCurves(
                        $logAvailableCurves,
                        is_string($requestedCurves) ? $requestedCurves : null,
                    );
                    ['tracks' => $logTracks, 'depth_max' => $logDepthMax] = $this->buildLogTracks(
                        (string) $sampleCollar->collar_id,
                        $logAvailableCurves,
                        $logSelectedCurves,
                    );
                }
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }

            // Project layer row counts — drives the Layers panel left rail. Each
            // entry has the layer label, the table it represents, and the row
            // count so the UI can dim layers with no data.
            $samplesCount = 0;
            $lithologyCount = 0;
            $sp = $this->openSavepoint();
            try {
                $lithologyCount = (int) DB::table('silver.lithology_logs as l')
                    ->join('silver.collars as c', 'l.collar_id', '=', 'c.collar_id')
                    ->where('c.project_id', $project->project_id)
                    ->count();
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }
            $sp = $this->openSavepoint();
            try {
                $samplesCount = (int) DB::table('silver.samples as s')
                    ->join('silver.collars as c', 's.collar_id', '=', 'c.collar_id')
                    ->where('c.project_id', $project->project_id)
                    ->count();
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }
            $savedViewsCount = 0;
            $sp = $this->openSavepoint();
            try {
                $savedViewsCount = (int) DB::table('silver.saved_map_views')
                    ->where('project_id', $project->project_id)
                    ->count();
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }
            // Per-thickness tier counts — drives the "Ore tier ≥ Nm" toggles.
            //
            // One aggregate, not three. This was a loop over [5, 10, 20] that
            // ran the same GROUP BY over gold.drillhole_intervals_visual each
            // time and then counted the groups in PHP with `->get()->count()`
            // — so every Workspace page load paid three full scans of the
            // project's intervals AND pulled one row per qualifying collar
            // across the wire three times, to produce three integers.
            // `->count()` on a grouped builder returns a count per group,
            // which is presumably why it was written that way; the shape that
            // actually works is to group in a subquery and count outside it.
            //
            // SUM(CASE ...) rather than COUNT(*) FILTER: the filter clause is
            // Postgres 9.4+ and SQLite 3.30+, and the test database is SQLite.
            $tierCounts = ['ore_5' => 0, 'ore_10' => 0, 'ore_20' => 0];
            $sp = $this->openSavepoint();
            try {
                $tiers = DB::selectOne(
                    'SELECT
                        SUM(CASE WHEN thickness_m >= 5  THEN 1 ELSE 0 END) AS ore_5,
                        SUM(CASE WHEN thickness_m >= 10 THEN 1 ELSE 0 END) AS ore_10,
                        SUM(CASE WHEN thickness_m >= 20 THEN 1 ELSE 0 END) AS ore_20
                       FROM (
                         SELECT collar_id, SUM(depth_to - depth_from) AS thickness_m
                           FROM gold.drillhole_intervals_visual
                          WHERE project_id = ?
                            AND lithology_code = ?
                          GROUP BY collar_id
                       ) AS hole_thickness',
                    [$project->project_id, 'DERIVED-ORE'],
                );

                $tierCounts = [
                    'ore_5' => (int) ($tiers->ore_5 ?? 0),
                    'ore_10' => (int) ($tiers->ore_10 ?? 0),
                    'ore_20' => (int) ($tiers->ore_20 ?? 0),
                ];
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }

            $aoiAvailable = $projectAoi !== null ? 1 : 0;

            // Counts for the Martin-served MVT layers.
            //
            // These IDS ARE LOAD-BEARING: WorkspaceMap toggles an MVT layer by
            // looking up `visibleLayers[def.id]` using the id from
            // resources/js/lib/mvtLayers.ts, and an id with no entry here
            // resolves to undefined -> false -> layout.visibility 'none'
            // forever. Before this block only `collars` and `traces` overlapped,
            // so EIGHT of the ten MVT layers could never be shown — including
            // every imported shapefile, DXF and Surpac string
            // (imported-points/lines/polygons) and all surface geochemistry.
            // The map looked empty and nothing said why.
            //
            // Each is wrapped because the table may not exist in every
            // environment; a missing count must not take the whole page down.
            $mvtCounts = ['spatial_features' => 0, 'geochem' => 0, 'workings' => 0, 'formations' => 0, 'boundaries' => 0, 'seismic' => 0, 'drill_traces' => 0];
            foreach ([
                // `traces` toggles ONLY the desurveyed MVT traces now (the
                // synthetic due-south ticks were removed, FE-7 / GIS-10), so
                // it counts trace rows, not collars.
                'drill_traces' => 'silver.drill_traces',
                'spatial_features' => 'silver.spatial_features',
                'geochem' => 'silver.geochemistry',
                'workings' => 'silver.historic_workings',
                'formations' => 'silver.geological_formations',
                'boundaries' => 'silver.project_boundaries',
                'seismic' => 'silver.seismic_surveys',
            ] as $key => $table) {
                $sp = $this->openSavepoint();
                try {
                    $mvtCounts[$key] = (int) DB::table($table)
                        ->where('project_id', $project->project_id)->count();
                    $this->releaseSavepoint($sp);
                } catch (\Throwable $e) {
                    $this->rollBackToSavepoint($sp);
                    Log::debug('workspace: MVT count unavailable', [
                        'table' => $table, 'error' => $e->getMessage(),
                    ]);
                }
            }

            $projectLayers = [
                ['id' => 'collars', 'label' => 'Collars', 'count' => $collars->count(), 'on' => true],
                ['id' => 'samples', 'label' => 'Ore-bearing holes only', 'count' => $oreHoleCount, 'on' => false],
                ['id' => 'ore_heatmap', 'label' => 'Ore heatmap', 'count' => $oreHoleCount, 'on' => false],
                ['id' => 'traces', 'label' => 'Drillhole traces', 'count' => $mvtCounts['drill_traces'], 'on' => false],
                ['id' => 'aoi', 'label' => 'Project AOI', 'count' => $aoiAvailable, 'on' => false],
                ['id' => 'tier_5', 'label' => 'Ore tier ≥ 5 m', 'count' => $tierCounts['ore_5'], 'on' => false],
                ['id' => 'tier_10', 'label' => 'Ore tier ≥ 10 m', 'count' => $tierCounts['ore_10'], 'on' => false],
                ['id' => 'tier_20', 'label' => 'Ore tier ≥ 20 m', 'count' => $tierCounts['ore_20'], 'on' => false],
                ['id' => 'lithology', 'label' => 'Lithology bands (logs only)', 'count' => $lithologyCount, 'on' => false],
                ['id' => 'sections', 'label' => 'Cross sections', 'count' => $sectionsCount, 'on' => false],
                ['id' => 'saved_views', 'label' => 'Saved views', 'count' => $savedViewsCount, 'on' => false],

                // ── Martin MVT layers ───────────────────────────────────────
                // ids must match resources/js/lib/mvtLayers.ts EXACTLY; see the
                // comment above $mvtCounts for what breaks when they do not.
                //
                // The three imported-* entries share one tile source
                // (pg_spatial_features_by_project emits points, lines and
                // polygons as separate ST_AsMVT layers because one MapLibre
                // layer has one type), so they share one count.
                //
                // `on => true` for imported features and geochem: they are what
                // a geologist has just uploaded, and defaulting them off means
                // a successful import still shows an empty map.
                ['id' => 'imported-points', 'label' => 'Imported points', 'count' => $mvtCounts['spatial_features'], 'on' => true],
                ['id' => 'imported-lines', 'label' => 'Imported lines', 'count' => $mvtCounts['spatial_features'], 'on' => true],
                ['id' => 'imported-polygons', 'label' => 'Imported areas', 'count' => $mvtCounts['spatial_features'], 'on' => true],
                ['id' => 'geochem', 'label' => 'Geochemistry samples', 'count' => $mvtCounts['geochem'], 'on' => true],
                ['id' => 'historic-workings', 'label' => 'Historic workings', 'count' => $mvtCounts['workings'], 'on' => false],
                ['id' => 'formations', 'label' => 'Mapped geology', 'count' => $mvtCounts['formations'], 'on' => false],
                ['id' => 'boundaries', 'label' => 'Claim boundaries', 'count' => $mvtCounts['boundaries'], 'on' => false],
                ['id' => 'seismic', 'label' => 'Seismic surveys', 'count' => $mvtCounts['seismic'], 'on' => false],
            ];

            // Chronostratigraphic column. Prefer project-specific formations from
            // silver.geological_formations when present (any project). Fall back
            // to a regional reference column scoped to the project's jurisdiction
            // so the LOGS panel always has stratigraphic context next to the
            // multi-log curves.
            $stratUnits = [];
            $stratSource = 'reference';
            $sp = $this->openSavepoint();
            try {
                $formations = DB::table('silver.geological_formations')
                    ->where('project_id', $project->project_id)
                    ->orderBy('age_ma_upper')
                    ->get(['formation_name', 'age_period', 'age_ma_lower', 'age_ma_upper', 'lithology_primary', 'properties']);
                if ($formations->isNotEmpty()) {
                    $stratSource = 'project';
                    $stratUnits = $formations->map(fn ($f) => [
                        'age' => $this->formatAgeRange($f->age_ma_lower, $f->age_ma_upper),
                        'age_period' => (string) ($f->age_period ?? ''),
                        'unit_name' => (string) $f->formation_name,
                        'color' => 'oklch(0.7 0.10 70)',
                        'lithology' => $f->lithology_primary ? (string) $f->lithology_primary : null,
                        'is_host' => false,
                        'is_unconformity' => false,
                        'notes' => [],
                    ])->all();
                }
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) { /* fallback */
                $this->rollBackToSavepoint($sp);
            }
            if (empty($stratUnits)) {
                $stratUnits = $this->referenceStratColumn($project);
            }

            // Retain country context for the regional reference stratigraphic column.
            $country = $this->resolveProjectCountry($project);

            // The heavy 3D payload is built at most once per request, and only
            // when one of its deferred props is actually requested. It runs
            // in its OWN RLS transaction: Inertia resolves deferred closures
            // in toResponse(), after this outer withWorkspaceRls() transaction
            // has committed, so without the wrap the GUC would be unset and the
            // fail-closed policies would return nothing.
            $threeD = null;
            $loadThreeD = function () use (&$threeD, $workspaceId, $project, $collars): array {
                return $threeD ??= $this->withWorkspaceRls(
                    $workspaceId,
                    fn (): array => $this->buildThreeDPayload($project, $collars),
                );
            };
            $deferThreeD = fn (string $key) => Inertia::defer(fn () => $loadThreeD()[$key], 'viz3d');

            // Where the map opens when no collar has a position: the extent of
            // everything else the project has on the map. Without it a
            // GIS-only delivery (shapefiles, geochem, claims) had no map at all
            // (FE-3).
            $projectExtent = $collars->contains(fn ($c) => isset($c->lat, $c->lng))
                ? null
                : $this->projectExtent((string) $project->project_id);

            return Inertia::render('Foundry/Workspace', [
                'project' => [
                    'project_id' => $project->project_id,
                    'project_name' => $project->project_name,
                    'slug' => $project->slug,
                    'company' => $project->company,
                    'commodity' => $project->commodity,
                    'region' => $project->region,
                    'crs_epsg' => $project->crs_epsg,
                    // Client cache key for the silver MVT tile URLs (&v=). The
                    // proxy serves those tiles with max-age=86400, so a URL
                    // that never changes kept new imports invisible for a day
                    // (FE-5). WorkspaceMap also bumps it live from Echo.
                    'data_version' => (int) ($project->data_version ?? 0),
                ],
                'project_extent' => $projectExtent,
                'project_summary' => [
                    'total_drilled_m' => round($totalDrilledM, 1),
                    'mean_td_m' => $meanTd !== null ? round($meanTd, 1) : null,
                    'ore_hole_count' => $oreHoleCount,
                    'total_ore_thickness_m' => round($totalOreThicknessM, 1),
                    'mean_u3o8_pct' => $meanU3o8Pct !== null ? round($meanU3o8Pct, 4) : null,
                ],
                'collars' => $collars->map(function ($c) use ($oreBandsByCollar) {
                    $ore = $oreBandsByCollar[(string) $c->collar_id] ?? ['count' => 0, 'thickness_m' => 0];

                    return [
                        'collar_id' => (string) $c->collar_id,
                        'hole_id' => (string) $c->hole_id,
                        'hole_id_canonical' => (string) ($c->hole_id_canonical ?? $c->hole_id),
                        'easting' => $c->easting !== null ? (float) $c->easting : null,
                        'northing' => $c->northing !== null ? (float) $c->northing : null,
                        'total_depth' => $c->total_depth !== null ? (float) $c->total_depth : null,
                        'lat' => isset($c->lat) ? (float) $c->lat : null,
                        'lng' => isset($c->lng) ? (float) $c->lng : null,
                        'ore_bands' => $ore['count'],
                        'ore_thickness_m' => $ore['thickness_m'],
                        // CC-01 Item 2 — spatial uncertainty triple. Forwarded
                        // as-is; the WorkspaceMap uncertainty-rings layer filter
                        // skips features whose spatial_uncertainty_m is null.
                        'spatial_uncertainty_m' => isset($c->spatial_uncertainty_m) ? (float) $c->spatial_uncertainty_m : null,
                        'crs_confidence' => isset($c->crs_confidence) ? (float) $c->crs_confidence : null,
                        'georef_method' => $c->georef_method ?? null,
                        // Orientation triple + classification — feeds the 3D
                        // Trajectories sub-view in MODE=3D.
                        'azimuth' => isset($c->azimuth) ? (float) $c->azimuth : null,
                        'dip' => isset($c->dip) ? (float) $c->dip : null,
                        'elevation' => isset($c->elevation) ? (float) $c->elevation : null,
                        'elevation_source' => isset($c->elevation)
                            ? ((bool) ($c->elevation_from_terrain ?? false) ? 'terrain' : 'file')
                            : null,
                        // Which terrain model gave the height (COLLAR_DEM_SOURCE),
                        // so the UI can name and credit it; null for a file elevation.
                        'elevation_dem_source' => isset($c->elevation) && (bool) ($c->elevation_from_terrain ?? false)
                            ? ($c->elevation_dem_source ?? null)
                            : null,
                        'hole_type' => $c->hole_type ?? null,
                        'status' => $c->status ?? null,
                    ];
                })->values(),
                'sections_count' => $sectionsCount,
                'intervals_count' => $intervalsCount,
                'structures_count' => $structuresCount,
                'structures_visual_count' => $structuresVisualCount,
                'well_log_curves_count' => $wellLogCurvesCount,
                'curve_summary' => $curveSummary->map(fn ($r) => [
                    'curve_name' => (string) $r->curve_name,
                    'curves' => (int) $r->curves,
                    'avg_samples' => (int) round((float) $r->avg_samples),
                ])->values(),
                'log_tracks' => $logTracks,
                'log_available_curves' => $logAvailableCurves,
                'log_selected_curves' => $logSelectedCurves,
                'log_curves_max' => self::MAX_LOG_TRACKS,
                'log_hole_id' => $logHoleId,
                'log_depth_max' => $logDepthMax > 0 ? $logDepthMax : 600.0,
                'log_hole_options' => array_map(fn ($o) => $o['hole_id'], $logHoleOptions),
                'log_hole_total_depth' => $logHoleTotalDepth,
                'log_hole_easting' => $logHoleEasting,
                'log_hole_northing' => $logHoleNorthing,
                'log_lithology_intervals' => $logLithologyIntervals,
                'log_alteration_intervals' => $logAlterationIntervals,
                'log_mineralization_intervals' => $logMineralizationIntervals,
                'log_tracks_truncated' => $logTracksTruncated,
                // 3D / STRUCTURE payload — one deferred group, see
                // buildThreeDPayload(). The page renders a skeleton for the
                // panels that read these until the group arrives.
                'first_holes_intervals' => $deferThreeD('first_holes_intervals'),
                'surveys_3d' => $deferThreeD('surveys_3d'),
                'structures_3d' => $deferThreeD('structures_3d'),
                'assay_composites_3d' => $deferThreeD('assay_composites_3d'),
                'assay_elements_3d' => $deferThreeD('assay_elements_3d'),
                'significant_intersections_3d' => $deferThreeD('significant_intersections_3d'),
                'structures_visual_3d' => $deferThreeD('structures_visual_3d'),
                'commodity_samples_3d' => $deferThreeD('commodity_samples_3d'),
                'commodity_keys_3d' => $deferThreeD('commodity_keys_3d'),
                'survey_holes_downsampled' => $deferThreeD('survey_holes_downsampled'),
                'project_layers' => $projectLayers,
                'project_aoi' => $projectAoi,
                'strat_units' => $stratUnits,
                'strat_source' => $stratSource,
                'project_country' => $country,
                'empty' => $collars->isEmpty(),
                // Which payload caps actually bit. `collars` bounds the map and
                // every per-hole panel; `interval_holes` is the (smaller) 3D
                // lithology cap, a prefix of the same collar set;
                // How many holes had survey stations thinned is part of the
                // deferred 3D group (`survey_holes_downsampled`), since it is
                // only known once the surveys are read.
                'truncation' => $this->truncationSummary(
                    $collars->count(),
                    $collarsTotal,
                    min(self::MAX_INTERVAL_HOLES, $collars->count()),
                ),
            ]);
        });
    }

    /**
     * Lon/lat bounding box of the project's non-collar map data, or null.
     *
     * Each table is read in its own savepoint (the LAR-4 helpers from
     * SetsWorkspaceRlsContext): a missing table must not abort the
     * surrounding RLS transaction (Postgres refuses every later statement
     * in an aborted transaction). Boxes outside WGS84 range are discarded —
     * a CAD file stored with model units as 4326 (GIS-11) would otherwise
     * send the map to "longitude 512,100".
     *
     * @return array{0: float, 1: float, 2: float, 3: float}|null [west, south, east, north]
     */
    private function projectExtent(string $projectId): ?array
    {
        $box = null;
        foreach ([
            'silver.spatial_features',
            'silver.project_boundaries',
            'silver.geochemistry',
            'silver.geological_formations',
            'silver.historic_workings',
        ] as $table) {
            $sp = $this->openSavepoint();
            try {
                $row = DB::selectOne(
                    'SELECT ST_XMin(x.e) AS w, ST_YMin(x.e) AS s, ST_XMax(x.e) AS e, ST_YMax(x.e) AS n '
                    .'FROM (SELECT ST_Extent(geom) AS e FROM '.$table.' WHERE project_id = ?) AS x',
                    [$projectId],
                );
                $this->releaseSavepoint($sp);
            } catch (\Throwable $e) {
                $this->rollBackToSavepoint($sp);
                Log::debug('workspace: extent unavailable', ['table' => $table, 'error' => $e->getMessage()]);

                continue;
            }
            if (! $row || $row->w === null) {
                continue;
            }
            [$w, $south, $east, $n] = [(float) $row->w, (float) $row->s, (float) $row->e, (float) $row->n];
            if ($w < -180 || $east > 180 || $south < -90 || $n > 90) {
                continue;
            }
            $box = $box === null
                ? [$w, $south, $east, $n]
                : [min($box[0], $w), min($box[1], $south), max($box[2], $east), max($box[3], $n)];
        }

        return $box;
    }

    /**
     * The 3D / STRUCTURE payload: interval bands, survey stations, structures,
     * assay composites, significant intersections, structure discs and
     * commodity samples, all for exactly the collars the page returned.
     *
     * Sent as ONE deferred Inertia group (`viz3d`) rather than inside the
     * initial page: on a 1,000-hole project this is several MB of JSON that
     * MAP mode — the default — never reads, and it used to sit in the HTML
     * `data-page` attribute so first paint waited on parsing it (FE-11,
     * 2026-09-29 audit). The LOGS-panel partial reloads no longer pay for it
     * either, since a closure is only run when its prop is requested.
     *
     * Must run inside withWorkspaceRls() (show()'s $loadThreeD does that, in
     * a transaction of its own). Each block has its own savepoint (LAR-4), so
     * one failing query degrades only its panel instead of aborting the
     * transaction and emptying every block after it.
     *
     * @param Collection<int, \stdClass> $collars
     *
     * @return array{first_holes_intervals: list<array<string, mixed>>, surveys_3d: list<array<string, mixed>>, structures_3d: list<array<string, mixed>>, assay_composites_3d: list<array<string, mixed>>, assay_elements_3d: list<array<string, mixed>>, significant_intersections_3d: list<array<string, mixed>>, structures_visual_3d: list<array<string, mixed>>, commodity_samples_3d: list<array<string, mixed>>, commodity_keys_3d: list<array<string, mixed>>, survey_holes_downsampled: int}
     */
    private function buildThreeDPayload(Project $project, Collection $collars): array
    {
        // All holes' lithology intervals — feeds both the 3D Plotly viewer
        // and the mini-strip 3D grid. Capped at MAX_INTERVAL_HOLES holes +
        // MAX_INTERVAL_BANDS_PER_HOLE bands/hole to keep payloads reasonable
        // (worst case ~16k records ≈ 2MB).
        // Each entry now also carries lat/lng + easting/northing so the
        // 3D viewer can position each cylinder in real space.
        $firstHolesIntervals = [];
        $sp = $this->openSavepoint();
        try {
            // A prefix of the SAME ordered collar set the map and every
            // other per-hole panel use, not a second independent query.
            $collarRows = $collars->take(self::MAX_INTERVAL_HOLES);
            // One windowed query for every hole's bands, instead of one
            // query per hole.
            //
            // This was a `foreach ($collarRows as $cr)` issuing a
            // separate SELECT per collar — up to 200 sequential round
            // trips on a single page load. withWorkspaceRls() wraps the
            // whole action in DB::transaction(), and PgBouncer runs in
            // transaction pooling, so one server connection stayed
            // pinned across all 200. A handful of concurrent workspace
            // loads was enough to exhaust the server-side pool and queue
            // every other query in the application behind them.
            //
            // ROW_NUMBER() reproduces the per-collar `ORDER BY
            // depth_from LIMIT 80` exactly; a plain `whereIn` with a
            // global LIMIT would not — one deep hole would eat the whole
            // budget and the rest would come back empty.
            $bandsByCollar = [];
            $collarIds = $collarRows->pluck('collar_id')->all();
            if ($collarIds !== []) {
                $ranked = DB::table('gold.drillhole_intervals_visual')
                    ->whereIn('collar_id', $collarIds)
                    ->where('interval_kind', 'lithology')
                    ->selectRaw(
                        'collar_id, depth_from, depth_to, lithology_code, color_hint, '
                        .'ROW_NUMBER() OVER (PARTITION BY collar_id ORDER BY depth_from) AS rn',
                    );

                foreach (
                    DB::query()->fromSub($ranked, 'ranked')
                        ->where('rn', '<=', self::MAX_INTERVAL_BANDS_PER_HOLE)
                        ->orderBy('collar_id')
                        ->orderBy('depth_from')
                        ->get() as $b
                ) {
                    $bandsByCollar[(string) $b->collar_id][] = [
                        'from' => (float) $b->depth_from,
                        'to' => (float) $b->depth_to,
                        'code' => (string) $b->lithology_code,
                        'color' => (string) $b->color_hint,
                    ];
                }
            }

            foreach ($collarRows as $cr) {
                $firstHolesIntervals[] = [
                    // Lets the 3D lithology view join this hole to its
                    // collar attitude + surveys for desurveying (FE-9).
                    'collar_id' => (string) $cr->collar_id,
                    'hole_id' => (string) ($cr->hole_id_canonical ?? $cr->hole_id),
                    'total_depth' => $cr->total_depth !== null ? (float) $cr->total_depth : null,
                    'easting' => $cr->easting !== null ? (float) $cr->easting : null,
                    'northing' => $cr->northing !== null ? (float) $cr->northing : null,
                    'lat' => isset($cr->lat) ? (float) $cr->lat : null,
                    'lng' => isset($cr->lng) ? (float) $cr->lng : null,
                    // A hole with no lithology bands still gets an entry,
                    // same as when its per-hole query returned nothing.
                    'bands' => $bandsByCollar[(string) $cr->collar_id] ?? [],
                ];
            }
            $this->releaseSavepoint($sp);
        } catch (\Throwable $e) { /* fallback empty */
            $this->rollBackToSavepoint($sp);
        }

        // Downhole survey stations (depth, azimuth, dip) — feeds the 3D
        // Trajectories sub-view in the workspace 3D mode.
        //
        // Fetched for EXACTLY the collars returned above (whereIn on that
        // set), bounded PER HOLE rather than by one global row cap. The old
        // `ORDER BY collar_id LIMIT 20000` spent the whole budget on the
        // first holes in uuid order and silently gave every later hole no
        // trajectory at all. Now a hole with more than
        // MAX_SURVEY_STATIONS_PER_HOLE stations is thinned to evenly spaced
        // stations (first and last always kept, so the deep end of the
        // trace is not cut off); the visual is a qualitative drill-pattern
        // check, not a precise survey export.
        //
        // FALLBACK, per collar: any returned collar with no silver.surveys
        // rows gets stations derived from silver.well_log_curves AZIMUTH +
        // SANG curves (Cameco binary .log corpus carries per-depth survey
        // angles on every hole but has never promoted them into the surveys
        // table), downsampled to ~25 stations per hole.
        $surveys = [];
        $surveyHolesDownsampled = 0;
        $sp = $this->openSavepoint();
        try {
            $collarIds = $collars->pluck('collar_id')->all();
            if (! empty($collarIds)) {
                $maxStations = self::MAX_SURVEY_STATIONS_PER_HOLE;
                $ranked = DB::table('silver.surveys')
                    ->whereIn('collar_id', $collarIds)
                    ->selectRaw(
                        'collar_id, depth, azimuth, dip, '
                        .'ROW_NUMBER() OVER (PARTITION BY collar_id ORDER BY depth) AS rn, '
                        .'COUNT(*) OVER (PARTITION BY collar_id) AS cnt',
                    );
                $surveyRows = DB::query()->fromSub($ranked, 'ranked')
                    ->whereRaw(sprintf(
                        '(((rn - 1) %% ((cnt + %1$d - 1) / %1$d)) = 0 OR rn = cnt)',
                        $maxStations,
                    ))
                    ->orderBy('collar_id')
                    ->orderBy('depth')
                    ->get(['collar_id', 'depth', 'azimuth', 'dip', 'cnt']);

                $downsampled = [];
                foreach ($surveyRows as $r) {
                    if ((int) $r->cnt > $maxStations) {
                        $downsampled[(string) $r->collar_id] = true;
                    }
                    $surveys[] = [
                        'collar_id' => (string) $r->collar_id,
                        'depth' => (float) $r->depth,
                        'azimuth' => $r->azimuth !== null ? (float) $r->azimuth : null,
                        'dip' => $r->dip !== null ? (float) $r->dip : null,
                    ];
                }
                $surveyHolesDownsampled = count($downsampled);

                $haveSurveys = array_fill_keys(array_column($surveys, 'collar_id'), true);
                $missing = array_values(array_filter(
                    array_map('strval', $collarIds),
                    fn (string $id) => ! isset($haveSurveys[$id]),
                ));
                // Chunked: each collar's AZIMUTH/SANG arrays are ~3.7k
                // samples, so bound how many are parsed in memory at once.
                foreach (array_chunk($missing, 100) as $chunk) {
                    array_push($surveys, ...$this->deriveSurveysFromCurves($chunk));
                }
            }
            $this->releaseSavepoint($sp);
        } catch (\Throwable $e) { /* fallback empty */
            $this->rollBackToSavepoint($sp);
        }

        // Raw structure measurements (planar features + lineations) — feeds
        // the 3D Stereosphere sub-view. Table is `silver.structure` (singular,
        // per the migration); columns are `true_dip` + `true_dip_dir` + `notes`.
        // May be empty (Wyoming Cameco binary .log corpus has no extracted
        // structures yet); Stereosphere still renders the wireframe.
        $structures = [];
        $sp = $this->openSavepoint();
        try {
            $collarIds = $collars->pluck('collar_id')->all();
            if (! empty($collarIds)) {
                $structures = DB::table('silver.structure')
                    ->whereIn('collar_id', $collarIds)
                    ->whereNotNull('true_dip')
                    ->whereNotNull('true_dip_dir')
                    ->limit(5000)
                    ->get(['collar_id', 'depth', 'structure_type', 'true_dip', 'true_dip_dir', 'notes'])
                    ->map(fn ($r) => [
                        'collar_id' => (string) $r->collar_id,
                        'depth' => (float) $r->depth,
                        'structure_type' => (string) $r->structure_type,
                        'true_dip' => $r->true_dip !== null ? (float) $r->true_dip : null,
                        'dip_direction' => $r->true_dip_dir !== null ? (float) $r->true_dip_dir : null,
                        'description' => $r->notes !== null ? (string) $r->notes : null,
                    ])
                    ->values()
                    ->all();
            }
            $this->releaseSavepoint($sp);
        } catch (\Throwable $e) { /* fallback empty */
            $this->rollBackToSavepoint($sp);
        }

        // Gold-tier 3D payloads — three new sub-views in MODE=3D:
        // assay grade bands, significant intersection highlights, and
        // structure-measurement discs. All wrapped in try/catch so the
        // route still renders cleanly if a table is missing or empty.

        // gold.assay_composites — composited grade bands per hole/element.
        // Default to the most-common element on the project so the picker
        // has a sensible starting state; the FE can switch.
        $assayComposites = [];
        $assayElements = [];
        $sp = $this->openSavepoint();
        try {
            $collarIds = $collars->pluck('collar_id')->all();
            if (! empty($collarIds)) {
                $elementRows = DB::table('gold.assay_composites')
                    ->whereIn('collar_id', $collarIds)
                    ->select('element', DB::raw('COUNT(*) AS n'))
                    ->groupBy('element')
                    ->orderByDesc('n')
                    ->limit(12)
                    ->get();
                $assayElements = $elementRows->map(fn ($r) => [
                    'element' => (string) $r->element,
                    'count' => (int) $r->n,
                ])->values()->all();

                $assayComposites = DB::table('gold.assay_composites')
                    ->whereIn('collar_id', $collarIds)
                    ->orderBy('collar_id')
                    ->orderBy('element')
                    ->orderBy('from_depth')
                    ->limit(10000)
                    ->get(['collar_id', 'element', 'from_depth', 'to_depth', 'weighted_avg', 'unit', 'cutoff_grade', 'sample_count'])
                    ->map(fn ($r) => [
                        'collar_id' => (string) $r->collar_id,
                        'element' => (string) $r->element,
                        'from_depth' => (float) $r->from_depth,
                        'to_depth' => (float) $r->to_depth,
                        'weighted_avg' => (float) $r->weighted_avg,
                        'unit' => (string) $r->unit,
                        'cutoff_grade' => $r->cutoff_grade !== null ? (float) $r->cutoff_grade : null,
                        'sample_count' => $r->sample_count !== null ? (int) $r->sample_count : null,
                    ])
                    ->values()
                    ->all();
            }
            $this->releaseSavepoint($sp);
        } catch (\Throwable $e) { /* fallback empty */
            $this->rollBackToSavepoint($sp);
        }

        // gold.significant_intersections — one or more cutoff-grade hits per
        // hole. Renders as a highlight ribbon on each trace.
        $significantIntersections = [];
        $sp = $this->openSavepoint();
        try {
            $collarIds = $collars->pluck('collar_id')->all();
            if (! empty($collarIds)) {
                $significantIntersections = DB::table('gold.significant_intersections')
                    ->whereIn('collar_id', $collarIds)
                    ->orderBy('collar_id')
                    ->orderBy('from_depth')
                    ->limit(5000)
                    ->get(['collar_id', 'element', 'cutoff_grade', 'from_depth', 'to_depth', 'true_width_m', 'weighted_avg', 'unit', 'peak_value', 'peak_depth', 'zone_name'])
                    ->map(fn ($r) => [
                        'collar_id' => (string) $r->collar_id,
                        'element' => (string) $r->element,
                        'cutoff_grade' => (float) $r->cutoff_grade,
                        'from_depth' => (float) $r->from_depth,
                        'to_depth' => (float) $r->to_depth,
                        'true_width_m' => $r->true_width_m !== null ? (float) $r->true_width_m : null,
                        'weighted_avg' => (float) $r->weighted_avg,
                        'unit' => (string) $r->unit,
                        'peak_value' => $r->peak_value !== null ? (float) $r->peak_value : null,
                        'peak_depth' => $r->peak_depth !== null ? (float) $r->peak_depth : null,
                        'zone_name' => $r->zone_name !== null ? (string) $r->zone_name : null,
                    ])
                    ->values()
                    ->all();
            }
            $this->releaseSavepoint($sp);
        } catch (\Throwable $e) { /* fallback empty */
            $this->rollBackToSavepoint($sp);
        }

        // gold.structure_measurements_visual — depth-anchored strike/dip
        // measurements with stereonet-ready derived columns. Feeds the
        // Structure Discs sub-view.
        $structuresVisual = [];
        $sp = $this->openSavepoint();
        try {
            // Real schema (verified 2026-05-25): columns are `depth` (not
            // depth_m), `structure_type` (not measurement_kind), `trend_deg`
            // / `plunge_deg` (not pole_*). Earlier migration source under
            // database/raw/_archive/phase5-30-structure-measurements-visual.sql
            // is stale relative to the live table (archived 2026-08-28).
            $structuresVisual = DB::table('gold.structure_measurements_visual')
                ->where('project_id', $project->project_id)
                ->whereNotNull('collar_id')
                ->orderBy('collar_id')
                ->orderBy('depth')
                ->limit(5000)
                ->get(['collar_id', 'strike_deg', 'dip_deg', 'structure_type', 'depth', 'trend_deg', 'plunge_deg', 'dip_direction_deg'])
                ->map(function ($r) {
                    // Pole-to-plane: trend = (dip_direction + 180) mod 360,
                    // plunge = 90 - dip. Fallback because the gold asset
                    // doesn't currently populate trend_deg / plunge_deg, but
                    // it does populate dip_direction_deg + dip_deg.
                    $dip = $r->dip_deg !== null ? (float) $r->dip_deg : 0.0;
                    $dipDir = $r->dip_direction_deg !== null ? (float) $r->dip_direction_deg : null;
                    $trend = $r->trend_deg !== null ? (float) $r->trend_deg
                        : ($dipDir !== null ? fmod($dipDir + 180.0, 360.0) : 0.0);
                    $plunge = $r->plunge_deg !== null ? (float) $r->plunge_deg : (90.0 - $dip);

                    return [
                        'collar_id' => (string) $r->collar_id,
                        'strike_deg' => $r->strike_deg !== null ? (float) $r->strike_deg : 0.0,
                        'dip_deg' => $dip,
                        'measurement_kind' => (string) $r->structure_type,
                        'depth_m' => $r->depth !== null ? (float) $r->depth : null,
                        'pole_trend_deg' => $trend,
                        'pole_plunge_deg' => $plunge,
                        'display_color' => null,
                        'display_symbol' => null,
                        'confidence' => null,
                    ];
                })
                ->values()
                ->all();
            $this->releaseSavepoint($sp);
        } catch (\Throwable $e) { /* fallback empty */
            $this->rollBackToSavepoint($sp);
        }

        // silver.samples (commodity-grade samples) — feeds the
        // CommoditySamples3DView sub-view. For Cameco this is the only
        // place uranium grade (U3O8_pct_e) appears at hole+depth resolution;
        // gold.assay_composites covers geochemistry/REE/base-metals but not
        // U. Available commodities are surfaced as a picker; default to the
        // one with the most non-null samples.
        $commoditySamples = [];
        $commodityKeys = [];
        $sp = $this->openSavepoint();
        try {
            $collarIds = $collars->pluck('collar_id')->all();
            if (! empty($collarIds)) {
                // Walk all sample rows and tally which jsonb keys carry a
                // numeric grade. We could push this into SQL with jsonb
                // path queries, but the 5k row cap keeps the PHP loop fast
                // and lets us be lenient with key normalisation.
                $rawSamples = DB::table('silver.samples')
                    ->whereIn('collar_id', $collarIds)
                    ->whereNotNull('commodity_assays')
                    ->orderBy('collar_id')
                    ->orderBy('from_depth')
                    ->limit(5000)
                    ->get(['collar_id', 'from_depth', 'to_depth', 'sample_type', 'commodity_assays']);

                $tally = [];
                foreach ($rawSamples as $r) {
                    $assays = is_string($r->commodity_assays) ? json_decode($r->commodity_assays, true) : $r->commodity_assays;
                    if (! is_array($assays)) {
                        continue;
                    }
                    foreach ($assays as $key => $val) {
                        if (! is_numeric($val)) {
                            continue;
                        }
                        $tally[$key] = ($tally[$key] ?? 0) + 1;
                    }
                }
                arsort($tally);
                // Exclude bookkeeping keys that aren't grades.
                $skip = ['confidence', 'n_points', 'method'];
                $commodityKeys = [];
                foreach ($tally as $k => $n) {
                    if (in_array($k, $skip, true)) {
                        continue;
                    }
                    $commodityKeys[] = ['key' => (string) $k, 'count' => (int) $n];
                }

                $commoditySamples = [];
                foreach ($rawSamples as $r) {
                    $assays = is_string($r->commodity_assays) ? json_decode($r->commodity_assays, true) : $r->commodity_assays;
                    if (! is_array($assays)) {
                        continue;
                    }
                    $values = [];
                    foreach ($assays as $key => $val) {
                        if (is_numeric($val) && ! in_array($key, $skip, true)) {
                            $values[$key] = (float) $val;
                        }
                    }
                    if (empty($values)) {
                        continue;
                    }
                    $commoditySamples[] = [
                        'collar_id' => (string) $r->collar_id,
                        'from_depth' => (float) $r->from_depth,
                        'to_depth' => (float) $r->to_depth,
                        'sample_type' => (string) $r->sample_type,
                        'grades' => $values,
                    ];
                }
            }
            $this->releaseSavepoint($sp);
        } catch (\Throwable $e) { /* fallback empty */
            $this->rollBackToSavepoint($sp);
        }

        return [
            'first_holes_intervals' => $firstHolesIntervals,
            'surveys_3d' => $surveys,
            'structures_3d' => $structures,
            'assay_composites_3d' => $assayComposites,
            'assay_elements_3d' => $assayElements,
            'significant_intersections_3d' => $significantIntersections,
            'structures_visual_3d' => $structuresVisual,
            'commodity_samples_3d' => $commoditySamples,
            'commodity_keys_3d' => $commodityKeys,
            'survey_holes_downsampled' => $surveyHolesDownsampled,
        ];
    }

    /**
     * JSON payload for one hole — drives the side-by-side comparison modal.
     * Returns the same shape we'd build for the LOGS panel: curve tracks,
     * lithology intervals, and the collar metadata. Fetched on demand by
     * the front-end when a user marks a hole for compare.
     *
     * GET /projects/{slug}/holes/{hole}/payload
     */
    public function holePayload(Request $request, string $slug, string $hole): JsonResponse
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $workspaceId = (string) $project->workspace_id;

        return $this->withWorkspaceRls($workspaceId, function () use ($request, $project, $hole) {
            $collar = DB::table('silver.collars')
                ->where('project_id', $project->project_id)
                ->where(function ($q) use ($hole) {
                    $q->where('hole_id', $hole)->orWhere('hole_id_canonical', $hole);
                })
                ->selectRaw('collar_id, hole_id, hole_id_canonical, easting, northing, total_depth, ST_X(geom_4326) AS lng, ST_Y(geom_4326) AS lat')
                ->first();

            if (! $collar) {
                return response()->json(['error' => 'hole_not_found', 'hole_id' => $hole], 404);
            }

            $availableCurves = $this->availableLogCurves((string) $collar->collar_id);
            $requestedCurves = $request->query('log_curves');
            $selectedCurves = $this->selectLogCurves(
                $availableCurves,
                is_string($requestedCurves) ? $requestedCurves : null,
            );
            ['tracks' => $logTracks, 'depth_max' => $logDepthMax] = $this->buildLogTracks(
                (string) $collar->collar_id,
                $availableCurves,
                $selectedCurves,
            );

            $strip = (new HoleStripTracks)->forCollar((string) $collar->collar_id);

            $oreStats = DB::table('gold.drillhole_intervals_visual')
                ->where('collar_id', $collar->collar_id)
                ->where('lithology_code', 'DERIVED-ORE')
                ->selectRaw('COUNT(*) AS n, COALESCE(SUM(depth_to - depth_from), 0) AS thickness')
                ->first();

            $meanGrade = DB::table('silver.samples')
                ->where('collar_id', $collar->collar_id)
                ->where('sample_type', 'derived_composite')
                ->selectRaw("AVG(NULLIF((commodity_assays->>'U3O8_pct_e')::numeric, 0)) AS mean_grade")
                ->first();

            return response()->json([
                'hole_id' => (string) ($collar->hole_id_canonical ?? $collar->hole_id),
                'collar_id' => (string) $collar->collar_id,
                'total_depth' => $collar->total_depth !== null ? (float) $collar->total_depth : null,
                'easting' => $collar->easting !== null ? (float) $collar->easting : null,
                'northing' => $collar->northing !== null ? (float) $collar->northing : null,
                'lat' => isset($collar->lat) ? (float) $collar->lat : null,
                'lng' => isset($collar->lng) ? (float) $collar->lng : null,
                'log_tracks' => $logTracks,
                'log_available_curves' => $availableCurves,
                'log_selected_curves' => $selectedCurves,
                'log_depth_max' => $logDepthMax > 0 ? $logDepthMax : 600.0,
                'lithology_intervals' => $strip['lithology'],
                'alteration_intervals' => $strip['alteration'],
                'mineralization_intervals' => $strip['mineralization'],
                'ore_bands' => (int) ($oreStats->n ?? 0),
                'ore_thickness_m' => round((float) ($oreStats->thickness ?? 0), 2),
                'mean_u3o8_pct' => $meanGrade && $meanGrade->mean_grade !== null
                    ? round((float) $meanGrade->mean_grade, 5)
                    : null,
            ]);
        });
    }

    /**
     * @return array{collars: array{shown: int, total: int, truncated: bool}, interval_holes: array{shown: int, total: int, truncated: bool}}
     */
    private function truncationSummary(int $collarsShown, int $collarsTotal, int $intervalHolesShown): array
    {
        $collarsTotal = max($collarsTotal, $collarsShown);

        return [
            'collars' => [
                'shown' => $collarsShown,
                'total' => $collarsTotal,
                'truncated' => $collarsShown < $collarsTotal,
            ],
            'interval_holes' => [
                'shown' => $intervalHolesShown,
                'total' => $collarsTotal,
                'truncated' => $intervalHolesShown < $collarsTotal,
            ],
        ];
    }

    /**
     * Resolve a curve name to its alias-group key ('other' when unlisted).
     * Case-insensitive; ordering, colour and unit fallback only.
     */
    private function logCurveGroup(string $curveName): string
    {
        $needle = strtoupper(trim($curveName));
        foreach (self::LOG_CURVE_GROUPS as $group => $def) {
            if (in_array($needle, $def['aliases'], true)) {
                return $group;
            }
        }

        return 'other';
    }

    /**
     * Every curve the hole has, in default-selection order: alias groups in
     * LOG_CURVE_GROUPS order (gamma family first), then everything else,
     * alphabetical within each. One cheap query — no depth/value arrays.
     *
     * @return list<array{curve_name: string, unit: string|null, group: string, sample_count: int}>
     */
    private function availableLogCurves(string $collarId): array
    {
        $rows = DB::table('silver.well_log_curves')
            ->where('collar_id', $collarId)
            ->orderBy('curve_name')
            ->get(['curve_name', 'curve_unit', 'sample_count']);

        $groupOrder = array_flip(array_keys(self::LOG_CURVE_GROUPS));
        $out = [];
        foreach ($rows as $r) {
            $name = (string) $r->curve_name;
            $group = $this->logCurveGroup($name);
            $out[] = [
                'curve_name' => $name,
                'unit' => $r->curve_unit !== null && trim((string) $r->curve_unit) !== '' ? (string) $r->curve_unit : null,
                'group' => $group,
                'sample_count' => (int) $r->sample_count,
            ];
        }
        usort($out, function (array $a, array $b) use ($groupOrder): int {
            $ga = $groupOrder[$a['group']] ?? PHP_INT_MAX;
            $gb = $groupOrder[$b['group']] ?? PHP_INT_MAX;

            return $ga <=> $gb ?: strcmp($a['curve_name'], $b['curve_name']);
        });

        return $out;
    }

    /**
     * Which curves to draw. `?log_curves=A,B` is honoured only for names the
     * hole actually has (so it cannot be used to probe other tables), capped
     * at MAX_LOG_TRACKS; with no valid request the first DEFAULT_LOG_TRACKS
     * of the priority-ordered list are drawn.
     *
     * @param list<array{curve_name: string, unit: string|null, group: string, sample_count: int}> $available
     *
     * @return list<string>
     */
    private function selectLogCurves(array $available, ?string $requestedCsv): array
    {
        $names = array_column($available, 'curve_name');

        if ($requestedCsv !== null && $requestedCsv !== '') {
            $wanted = array_flip(array_map('trim', explode(',', $requestedCsv)));
            $picked = array_values(array_filter($names, fn (string $n) => isset($wanted[$n])));
            if ($picked !== []) {
                return array_slice($picked, 0, self::MAX_LOG_TRACKS);
            }
        }

        return array_slice($names, 0, self::DEFAULT_LOG_TRACKS);
    }

    /**
     * Downsampled tracks for the selected curves, in `$selected` order, in one
     * query. Curves that are entirely the null sentinel produce no track.
     *
     * @param list<array{curve_name: string, unit: string|null, group: string, sample_count: int}> $available
     * @param list<string> $selected
     *
     * @return array{tracks: list<array<string, mixed>>, depth_max: float}
     */
    private function buildLogTracks(string $collarId, array $available, array $selected): array
    {
        if ($selected === []) {
            return ['tracks' => [], 'depth_max' => 0.0];
        }

        $meta = [];
        foreach ($available as $a) {
            $meta[$a['curve_name']] = $a;
        }

        $rows = DB::table('silver.well_log_curves')
            ->where('collar_id', $collarId)
            ->whereIn('curve_name', $selected)
            ->select('curve_name', 'depths', 'values', 'max_depth', 'null_value')
            ->get()
            ->keyBy(fn ($r) => (string) $r->curve_name);

        $tracks = [];
        $depthMax = 0.0;
        foreach ($selected as $name) {
            $row = $rows->get($name);
            if (! $row) {
                continue;
            }
            $depths = $this->parsePgDoubleArray($row->depths);
            $values = $this->parsePgDoubleArray($row->values);
            $n = min(count($depths), count($values));
            if ($n === 0) {
                continue;
            }
            $step = max(1, (int) floor($n / self::LOG_CURVE_TARGET_POINTS));
            $pts = [];
            $vmin = INF;
            $vmax = -INF;
            for ($i = 0; $i < $n; $i += $step) {
                $v = $values[$i];
                // null_value sentinel (commonly -999.25) — skip
                if (abs($v - (float) $row->null_value) < 1e-6) {
                    continue;
                }
                $pts[] = ['depth' => $depths[$i], 'value' => $v];
                $vmin = min($vmin, $v);
                $vmax = max($vmax, $v);
            }
            if ($pts === []) {
                continue;
            }

            $unit = $meta[$name]['unit'] ?? null;
            $group = $meta[$name]['group'] ?? 'other';
            $def = self::LOG_CURVE_GROUPS[$group] ?? null;
            $unitLabel = $unit ?? ($def['unit'] ?? null);
            $colors = self::LOG_CURVE_FALLBACK_COLORS;

            $tracks[] = [
                'curve' => $name,
                'group' => $group,
                'unit' => $unit,
                'label' => $unitLabel !== null ? sprintf('%s (%s)', $name, $unitLabel) : $name,
                'color' => $def['color'] ?? $colors[crc32($name) % count($colors)],
                'points' => $pts,
                'min' => is_finite($vmin) ? $vmin : 0,
                'max' => is_finite($vmax) ? $vmax : 1,
            ];
            $depthMax = max($depthMax, (float) $row->max_depth);
        }

        return ['tracks' => $tracks, 'depth_max' => $depthMax];
    }

    private function formatAgeRange(mixed $lower, mixed $upper): string
    {
        $l = $lower !== null ? (float) $lower : null;
        $u = $upper !== null ? (float) $upper : null;
        if ($l === null && $u === null) {
            return '—';
        }
        if ($l !== null && $u !== null) {
            return sprintf('%s–%s Ma', $this->formatMa($u), $this->formatMa($l));
        }

        return ($l ?? $u) !== null ? sprintf('%s Ma', $this->formatMa($l ?? $u)) : '—';
    }

    private function formatMa(?float $v): string
    {
        if ($v === null) {
            return '—';
        }
        if ($v >= 100) {
            return (string) (int) round($v);
        }

        return rtrim(rtrim(number_format($v, 2, '.', ''), '0'), '.');
    }

    /**
     * Regional reference chronostratigraphic column for the project's
     * jurisdiction. Used when silver.geological_formations has no project
     * rows. The Wyoming column reflects roll-front sandstone-hosted uranium
     * country (Shirley / Powder River / Wind River basins); the Canadian
     * column reflects the Athabasca Basin / Wollaston Domain.
     */
    private function referenceStratColumn(Project $project): array
    {
        $country = $this->resolveProjectCountry($project);
        if ($country === 'US') {
            return [
                ['age' => '0–2.6 Ma', 'age_period' => 'Quaternary', 'unit_name' => 'Alluvium / colluvium', 'color' => 'oklch(0.88 0.04 90)', 'lithology' => 'Unconsolidated sediment', 'is_host' => false, 'is_unconformity' => false, 'notes' => ['Surficial cover']],
                ['age' => '~48–37 Ma', 'age_period' => 'Eocene', 'unit_name' => 'Wagon Bed Fm', 'color' => 'oklch(0.80 0.08 85)', 'lithology' => 'Tuffaceous mudstone / sandstone', 'is_host' => false, 'is_unconformity' => false, 'notes' => ['Overburden above U host']],
                ['age' => '~52–48 Ma', 'age_period' => 'Eocene', 'unit_name' => 'Wind River Fm', 'color' => 'oklch(0.78 0.13 70)', 'lithology' => 'Fluvial channel sandstone + mudstone', 'is_host' => true, 'is_unconformity' => false, 'notes' => ['Primary roll-front U host', 'Reducing facies + organic C']],
                ['age' => '~66–52 Ma', 'age_period' => 'Paleocene', 'unit_name' => 'Fort Union Fm', 'color' => 'oklch(0.70 0.10 60)', 'lithology' => 'Sandstone, coal, mudstone', 'is_host' => true, 'is_unconformity' => false, 'notes' => ['Roll-front host in Powder River Basin']],
                ['age' => '~70 Ma', 'age_period' => 'K–Pg', 'unit_name' => 'UNCONFORMITY', 'color' => 'oklch(0.55 0.04 50)', 'lithology' => null, 'is_host' => false, 'is_unconformity' => true, 'notes' => ['Cretaceous–Tertiary erosion']],
                ['age' => '~75–66 Ma', 'age_period' => 'Late Cretaceous', 'unit_name' => 'Lance / Fox Hills Sst', 'color' => 'oklch(0.60 0.10 240)', 'lithology' => 'Marginal-marine sandstone', 'is_host' => false, 'is_unconformity' => false, 'notes' => ['Underlying clastic wedge']],
                ['age' => '>~94 Ma', 'age_period' => 'Cretaceous', 'unit_name' => 'Cody / Mesaverde', 'color' => 'oklch(0.50 0.05 250)', 'lithology' => 'Marine shale, sandstone', 'is_host' => false, 'is_unconformity' => false, 'notes' => ['Regional seal']],
                ['age' => '>2500 Ma', 'age_period' => 'Archean', 'unit_name' => 'Precambrian basement', 'color' => 'oklch(0.40 0.05 280)', 'lithology' => 'Granitoid + gneiss', 'is_host' => false, 'is_unconformity' => false, 'notes' => ['Wyoming Province crust']],
            ];
        }

        // Canadian default — Athabasca / Wollaston Domain.
        return [
            ['age' => '0–2.6 Ma', 'age_period' => 'Quaternary', 'unit_name' => 'Glacial cover / till', 'color' => 'oklch(0.85 0.05 95)', 'lithology' => 'Till', 'is_host' => false, 'is_unconformity' => false, 'notes' => ['Surficial']],
            ['age' => '~1700–1500 Ma', 'age_period' => 'Proterozoic', 'unit_name' => 'Athabasca Group sandstone', 'color' => 'oklch(0.74 0.12 65)', 'lithology' => 'Fluvial-braided sandstone', 'is_host' => false, 'is_unconformity' => false, 'notes' => ['MFb fluvial sandstone', 'MFa basal pelite']],
            ['age' => '~1810 Ma', 'age_period' => 'Hudsonian', 'unit_name' => 'UNCONFORMITY', 'color' => 'oklch(0.55 0.04 50)', 'lithology' => null, 'is_host' => true, 'is_unconformity' => true, 'notes' => ['Basement weathering', 'Regolith chlorite-illite']],
            ['age' => '~2050–1850 Ma', 'age_period' => 'Paleoproterozoic', 'unit_name' => 'Wollaston Group pelites', 'color' => 'oklch(0.55 0.10 285)', 'lithology' => 'Graphitic pelite', 'is_host' => false, 'is_unconformity' => false, 'notes' => ['Graphitic pelite reductant', 'Hudsonian D2 deformation']],
            ['age' => '>2500 Ma', 'age_period' => 'Archean', 'unit_name' => 'Mudjatik basement', 'color' => 'oklch(0.40 0.05 280)', 'lithology' => 'Felsic gneiss + granitoid', 'is_host' => false, 'is_unconformity' => false, 'notes' => []],
        ];
    }

    /**
     * Fallback survey-station builder for projects whose `silver.surveys`
     * table is empty but whose `silver.well_log_curves` carry AZIMUTH +
     * SANG (survey angle = dip) curves per depth sample. This is the case
     * for the entire Cameco binary `.log` corpus — every hole has 3 700+
     * per-depth angle samples that were never promoted into surveys.
     *
     * We downsample to ~25 stations per hole, drop null-sentinel values,
     * and emit the same shape the silver.surveys → MultiHole3DTrace /
     * OrientationSpiral pipeline expects.
     *
     * @param list<string> $collarIds
     *
     * @return list<array{collar_id: string, depth: float, azimuth: float|null, dip: float|null}>
     */
    private function deriveSurveysFromCurves(array $collarIds): array
    {
        if (empty($collarIds)) {
            return [];
        }

        // Pull AZIMUTH + SANG curves for the requested collars in one
        // round-trip; pair them up in PHP. SANGB is the alternate SANG
        // (backup tool) — prefer SANG, fall back to SANGB.
        $rows = DB::table('silver.well_log_curves')
            ->whereIn('collar_id', $collarIds)
            ->whereIn('curve_name', ['AZIMUTH', 'SANG', 'SANGB'])
            ->select('collar_id', 'curve_name', 'depths', 'values', 'null_value')
            ->get();

        $byCollar = [];
        foreach ($rows as $r) {
            $cid = (string) $r->collar_id;
            $byCollar[$cid] ??= [];
            $byCollar[$cid][(string) $r->curve_name] = $r;
        }

        $out = [];
        $stationsPerHole = 25;
        foreach ($byCollar as $cid => $curves) {
            $az = $curves['AZIMUTH'] ?? null;
            $dip = $curves['SANG'] ?? $curves['SANGB'] ?? null;
            if (! $az || ! $dip) {
                continue;
            }

            $azDepths = $this->parsePgDoubleArray($az->depths);
            $azValues = $this->parsePgDoubleArray($az->values);
            $azNull = (float) $az->null_value;
            $dipDepths = $this->parsePgDoubleArray($dip->depths);
            $dipValues = $this->parsePgDoubleArray($dip->values);
            $dipNull = (float) $dip->null_value;

            // Use the AZIMUTH depths as the master grid and look up SANG
            // by index (curves are emitted at identical depth steps in
            // this corpus — verified in the Cameco binary parser).
            $n = min(count($azDepths), count($azValues), count($dipDepths), count($dipValues));
            if ($n < 2) {
                continue;
            }
            $step = max(1, (int) floor($n / $stationsPerHole));
            for ($i = 0; $i < $n; $i += $step) {
                $a = (float) $azValues[$i];
                $d = (float) $dipValues[$i];
                if (abs($a - $azNull) < 1e-6 || abs($d - $dipNull) < 1e-6) {
                    continue;
                }
                $out[] = [
                    'collar_id' => $cid,
                    'depth' => (float) $azDepths[$i],
                    'azimuth' => $a,
                    // SANG is the survey angle: 0 = vertical (straight down),
                    // 90 = horizontal. The trajectory integrator
                    // (resources/js/lib/desurvey.ts) takes `dip` in the
                    // silver convention — degrees from horizontal, negative
                    // = down — and since up-holes became legal (§04e,
                    // 2026-09-29) it honours the sign, so this must be
                    // negative for a down-going hole: dip = SANG - 90. It
                    // used to be 90 - SANG, which only worked while the
                    // integrator ignored the sign.
                    'dip' => $d - 90.0,
                ];
            }
        }

        return $out;
    }

    /**
     * Parse a PostgreSQL `double precision[]` column to a PHP float array.
     *
     * The PDO driver returns these as the postgres array literal string
     * `{0.2,0.3,0.4,...}`, not as JSON. json_decode returns null on that
     * format, which is the bug that made every LOG track render empty.
     *
     * @return list<float>
     */
    private function parsePgDoubleArray(mixed $raw): array
    {
        if (is_array($raw)) {
            return array_map('floatval', $raw);
        }
        if ($raw === null) {
            return [];
        }
        $s = trim((string) $raw);
        if ($s === '' || $s === '{}') {
            return [];
        }
        if ($s[0] === '{' && $s[-1] === '}') {
            $s = substr($s, 1, -1);
        }
        if ($s === '') {
            return [];
        }

        return array_map('floatval', explode(',', $s));
    }

    /**
     * Resolve a project's country code ('CA' | 'US' | 'OTHER') for the
     * purpose of selecting regional stratigraphic columns.
     *
     * Today silver.projects has no jurisdiction/country column, so we fall
     * back to name-based detection. Wyoming Cameco Shirley Basin → 'US'.
     * Saskatchewan/Ontario/BC/... project names → 'CA'. Anything else
     * defaults to 'CA' to preserve existing behavior for the seeded
     * Canadian regional context.
     *
     * TODO: replace with a real `silver.projects.country_code` column once
     * Module 10 doc-sweep lands.
     */
    private function resolveProjectCountry(Project $project): string
    {
        $haystack = strtolower($project->project_name.' '.($project->slug ?? ''));

        $usHints = ['wyoming', 'shirley basin', 'powder river', 'cameco shirley',
            'gas hills', 'wind river basin', 'nevada', 'arizona', 'utah',
            'colorado', 'new mexico', 'crook county', 'carbon county', ' usa', ' us '];
        foreach ($usHints as $hint) {
            if (str_contains($haystack, $hint)) {
                return 'US';
            }
        }

        $caHints = ['saskatchewan', 'athabasca', 'cigar lake', 'mcarthur', 'ontario',
            'british columbia', ' bc ', ' sk ', ' on ', 'quebec', 'manitoba',
            'alberta', 'nova scotia', 'newfoundland', 'thompson nickel',
            'red lake', 'sudbury'];
        foreach ($caHints as $hint) {
            if (str_contains($haystack, $hint)) {
                return 'CA';
            }
        }

        return 'CA';
    }
}
