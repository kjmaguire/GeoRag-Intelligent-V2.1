<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/WorkspaceController — project workspace with 5-mode switcher
 * (MAP / SECTION / 3D / STRUCTURE / LOGS).
 */
class WorkspaceController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $collars = DB::table('silver.collars')
            ->where('project_id', $project->project_id)
            // CC-01 Item 2 — surface spatial uncertainty + CRS provenance
            // so WorkspaceMap can render the uncertainty-rings layer per row.
            // Orientation triple (azimuth/dip/elevation) + hole_type/status
            // feed the 3D Trajectories sub-view (MultiHole3DTrace).
            ->selectRaw('collar_id, hole_id, hole_id_canonical, easting, northing, total_depth, ST_X(geom_4326) AS lng, ST_Y(geom_4326) AS lat, spatial_uncertainty_m, crs_confidence, georef_method, azimuth, dip, elevation, hole_type, status')
            ->limit(500)
            ->get();

        // Project summary aggregates — drives the map-overlay header and the
        // bottom stats chip. Computed across the project's collars + derived
        // ore-band rows; cheap (one aggregate query each).
        $totalDrilledM = 0.0;
        $meanTd = null;
        try {
            $td = DB::table('silver.collars')
                ->where('project_id', $project->project_id)
                ->selectRaw('COALESCE(SUM(total_depth), 0) AS sum_m, AVG(total_depth) AS avg_m')
                ->first();
            $totalDrilledM = (float) ($td->sum_m ?? 0);
            $meanTd = $td->avg_m !== null ? (float) $td->avg_m : null;
        } catch (\Throwable $e) { /* fallback */
        }

        $totalOreThicknessM = 0.0;
        $oreHoleCount = 0;
        $meanU3o8Pct = null;
        try {
            $ore = DB::table('gold.drillhole_intervals_visual')
                ->where('project_id', $project->project_id)
                ->where('lithology_code', 'DERIVED-ORE')
                ->selectRaw('COALESCE(SUM(depth_to - depth_from), 0) AS sum_m, COUNT(DISTINCT collar_id) AS holes')
                ->first();
            $totalOreThicknessM = (float) ($ore->sum_m ?? 0);
            $oreHoleCount = (int) ($ore->holes ?? 0);
        } catch (\Throwable $e) { /* fallback */
        }

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
        } catch (\Throwable $e) { /* fallback */
        }

        // Project AOI — convex hull of all collar geometries, as GeoJSON.
        // Drives the "Project AOI" toggle on the map (dashed outline).
        $projectAoi = null;
        try {
            $hullRow = DB::table('silver.collars')
                ->where('project_id', $project->project_id)
                ->whereNotNull('geom_4326')
                ->selectRaw('ST_AsGeoJSON(ST_ConvexHull(ST_Collect(geom_4326))) AS hull')
                ->first();
            if ($hullRow && $hullRow->hull) {
                $projectAoi = json_decode((string) $hullRow->hull, true);
            }
        } catch (\Throwable $e) { /* fallback */
        }

        // Ore-band counts per collar — drives the marker styling on the
        // MapLibre layer (mineralised holes are surfaced with a brighter
        // halo). One quick aggregate query keyed by collar_id.
        $oreBandsByCollar = [];
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
        } catch (\Throwable $e) { /* fallback empty */
        }

        $sectionsCount = 0;
        try {
            $sectionsCount = (int) DB::table('gold.cross_section_panels')
                ->where('project_id', $project->project_id)->count();
        } catch (\Throwable $e) { /* may not exist */
        }

        $intervalsCount = 0;
        try {
            $intervalsCount = (int) DB::table('gold.drillhole_intervals_visual')
                ->where('project_id', $project->project_id)->count();
        } catch (\Throwable $e) { /* may not exist */
        }

        $structuresVisualCount = 0;
        try {
            $structuresVisualCount = (int) DB::table('gold.structure_measurements_visual')
                ->where('project_id', $project->project_id)->count();
        } catch (\Throwable $e) { /* may not exist */
        }

        // Raw silver-tier structures (joined via collars). Empty for Wyoming
        // today — Cameco binary `.log` parse phase hasn't extracted measured
        // structures yet. But the AZIMUTH/SANG downhole survey curves on
        // well_log_curves *do* carry deviation angles we can surface as a
        // proxy for orientation context.
        $structuresCount = 0;
        try {
            $structuresCount = (int) DB::table('silver.structure as st')
                ->join('silver.collars as c', 'st.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $project->project_id)
                ->count();
        } catch (\Throwable $e) { /* fallback */
        }

        // Curve type summary — drives the LOGS mode legend.
        $curveSummary = collect();
        try {
            $curveSummary = DB::table('silver.well_log_curves as wc')
                ->join('silver.collars as c', 'wc.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $project->project_id)
                ->select('wc.curve_name', DB::raw('COUNT(*) as curves'), DB::raw('AVG(wc.sample_count) as avg_samples'))
                ->groupBy('wc.curve_name')
                ->orderByDesc('curves')
                ->limit(20)
                ->get();
        } catch (\Throwable $e) { /* fallback */
        }
        $wellLogCurvesCount = $curveSummary->sum('curves');

        // Build the list of collars that have at least one GAMMA curve —
        // this is what the LOGS hole picker shows. Ordered by hole_id so
        // the dropdown is predictable.
        $logHoleOptions = [];
        try {
            $logHoleOptions = DB::table('silver.well_log_curves as wc')
                ->join('silver.collars as c', 'wc.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $project->project_id)
                ->where('wc.curve_name', 'GAMMA')
                ->select('c.collar_id', 'c.hole_id', 'c.hole_id_canonical')
                ->orderBy('c.hole_id_canonical')
                ->orderBy('c.hole_id')
                ->get()
                ->map(fn ($r) => [
                    'collar_id' => (string) $r->collar_id,
                    'hole_id' => (string) ($r->hole_id_canonical ?? $r->hole_id),
                ])
                ->values()
                ->all();
        } catch (\Throwable $e) { /* fallback */
        }

        // Pull the selected (or first) hole's GAMMA/GRADE/RES/SP curves and
        // render them in the LOGS panel. The ?log_hole= query param overrides
        // the default picker selection.
        $logTracks = [];
        $logHoleId = null;
        $logDepthMax = 0.0;
        $logHoleTotalDepth = null;
        $logHoleEasting = null;
        $logHoleNorthing = null;
        try {
            $requestedHole = $request->query('log_hole');
            $sampleCollar = null;
            if ($requestedHole && ! empty($logHoleOptions)) {
                foreach ($logHoleOptions as $opt) {
                    if ($opt['hole_id'] === $requestedHole) {
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
            $logLithologyIntervals = [];
            if ($sampleCollar) {
                $logLithologyIntervals = DB::table('gold.drillhole_intervals_visual')
                    ->where('collar_id', $sampleCollar->collar_id)
                    ->where('interval_kind', 'lithology')
                    ->orderBy('depth_from')
                    ->get(['depth_from', 'depth_to', 'lithology_code', 'lithology_label', 'color_hint'])
                    ->map(fn ($r) => [
                        'from' => (float) $r->depth_from,
                        'to' => (float) $r->depth_to,
                        'code' => (string) $r->lithology_code,
                        'label' => (string) $r->lithology_label,
                        'color' => (string) $r->color_hint,
                    ])->values()->all();
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
                $wanted = [
                    ['curve' => 'GAMMA', 'label' => 'Gamma (cps)', 'color' => 'oklch(0.78 0.16 30)'],
                    ['curve' => 'GRADE', 'label' => 'U grade (%eU₃O₈)', 'color' => 'oklch(0.82 0.18 145)'],
                    ['curve' => 'RES', 'label' => 'Resistivity (Ω·m)', 'color' => 'oklch(0.72 0.14 220)'],
                    ['curve' => 'SP', 'label' => 'SP (mV)', 'color' => 'oklch(0.65 0.10 280)'],
                ];
                foreach ($wanted as $w) {
                    $row = DB::table('silver.well_log_curves')
                        ->where('collar_id', $sampleCollar->collar_id)
                        ->where('curve_name', $w['curve'])
                        ->select('depths', 'values', 'min_depth', 'max_depth', 'sample_count', 'null_value', 'curve_unit')
                        ->first();
                    if (! $row) {
                        continue;
                    }
                    $depths = $this->parsePgDoubleArray($row->depths);
                    $values = $this->parsePgDoubleArray($row->values);
                    // Downsample to ~240 pts for SVG perf.
                    $n = min(count($depths), count($values));
                    if ($n === 0) {
                        continue;
                    }
                    $step = max(1, (int) floor($n / 240));
                    $pts = [];
                    $vmin = INF;
                    $vmax = -INF;
                    for ($i = 0; $i < $n; $i += $step) {
                        $v = (float) $values[$i];
                        // null_value sentinel (commonly -999.25) — skip
                        if (abs($v - (float) $row->null_value) < 1e-6) {
                            continue;
                        }
                        $pts[] = ['depth' => (float) $depths[$i], 'value' => $v];
                        $vmin = min($vmin, $v);
                        $vmax = max($vmax, $v);
                    }
                    if (empty($pts)) {
                        continue;
                    }
                    $logTracks[] = [
                        'label' => $w['label'],
                        'color' => $w['color'],
                        'points' => $pts,
                        'min' => is_finite($vmin) ? $vmin : 0,
                        'max' => is_finite($vmax) ? $vmax : 1,
                    ];
                    $logDepthMax = max($logDepthMax, (float) $row->max_depth);
                }
            }
        } catch (\Throwable $e) { /* fallback */
        }

        // All holes' lithology intervals — feeds both the 3D Plotly viewer
        // and the mini-strip 3D grid. Capped at 200 holes + 80 bands/hole
        // to keep payloads reasonable (worst case ~16k records ≈ 2MB).
        // Each entry now also carries lat/lng + easting/northing so the
        // 3D viewer can position each cylinder in real space.
        $firstHolesIntervals = [];
        try {
            $collarRows = DB::table('silver.collars')
                ->where('project_id', $project->project_id)
                ->orderBy('hole_id')
                ->limit(200)
                ->selectRaw('collar_id, hole_id, hole_id_canonical, total_depth, easting, northing, ST_X(geom_4326) AS lng, ST_Y(geom_4326) AS lat')
                ->get();
            foreach ($collarRows as $cr) {
                $bands = DB::table('gold.drillhole_intervals_visual')
                    ->where('collar_id', $cr->collar_id)
                    ->where('interval_kind', 'lithology')
                    ->orderBy('depth_from')
                    ->limit(80)
                    ->get(['depth_from', 'depth_to', 'lithology_code', 'color_hint']);
                $firstHolesIntervals[] = [
                    'hole_id' => (string) ($cr->hole_id_canonical ?? $cr->hole_id),
                    'total_depth' => $cr->total_depth !== null ? (float) $cr->total_depth : null,
                    'easting' => $cr->easting !== null ? (float) $cr->easting : null,
                    'northing' => $cr->northing !== null ? (float) $cr->northing : null,
                    'lat' => isset($cr->lat) ? (float) $cr->lat : null,
                    'lng' => isset($cr->lng) ? (float) $cr->lng : null,
                    'bands' => $bands->map(fn ($b) => [
                        'from' => (float) $b->depth_from,
                        'to' => (float) $b->depth_to,
                        'code' => (string) $b->lithology_code,
                        'color' => (string) $b->color_hint,
                    ])->values()->all(),
                ];
            }
        } catch (\Throwable $e) { /* fallback empty */
        }

        // Downhole survey stations (depth, azimuth, dip) — feeds the 3D
        // Trajectories sub-view in the workspace 3D mode. Capped per-hole
        // and overall to keep the Inertia payload bounded; the visual is a
        // qualitative drill-pattern check, not a precise survey export.
        //
        // FALLBACK: when silver.surveys is empty for a collar, try to derive
        // stations from silver.well_log_curves AZIMUTH + SANG curves (Cameco
        // binary .log corpus carries per-depth survey angles on every hole
        // but has never promoted them into the surveys table). We downsample
        // to ~25 stations per hole — plenty for a qualitative trajectory.
        $surveys = [];
        try {
            $collarIds = $collars->pluck('collar_id')->all();
            if (! empty($collarIds)) {
                $surveys = DB::table('silver.surveys')
                    ->whereIn('collar_id', $collarIds)
                    ->orderBy('collar_id')
                    ->orderBy('depth')
                    ->limit(20000)
                    ->get(['collar_id', 'depth', 'azimuth', 'dip'])
                    ->map(fn ($r) => [
                        'collar_id' => (string) $r->collar_id,
                        'depth' => (float) $r->depth,
                        'azimuth' => $r->azimuth !== null ? (float) $r->azimuth : null,
                        'dip' => $r->dip !== null ? (float) $r->dip : null,
                    ])
                    ->values()
                    ->all();
            }
            if (empty($surveys) && ! empty($collarIds)) {
                $surveys = $this->deriveSurveysFromCurves($collarIds);
            }
        } catch (\Throwable $e) { /* fallback empty */
        }

        // Raw structure measurements (planar features + lineations) — feeds
        // the 3D Stereosphere sub-view. Table is `silver.structure` (singular,
        // per the migration); columns are `true_dip` + `true_dip_dir` + `notes`.
        // May be empty (Wyoming Cameco binary .log corpus has no extracted
        // structures yet); Stereosphere still renders the wireframe.
        $structures = [];
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
        } catch (\Throwable $e) { /* fallback empty */
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
        } catch (\Throwable $e) { /* fallback empty */
        }

        // gold.significant_intersections — one or more cutoff-grade hits per
        // hole. Renders as a highlight ribbon on each trace.
        $significantIntersections = [];
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
        } catch (\Throwable $e) { /* fallback empty */
        }

        // gold.structure_measurements_visual — depth-anchored strike/dip
        // measurements with stereonet-ready derived columns. Feeds the
        // Structure Discs sub-view.
        $structuresVisual = [];
        try {
            // Real schema (verified 2026-05-25): columns are `depth` (not
            // depth_m), `structure_type` (not measurement_kind), `trend_deg`
            // / `plunge_deg` (not pole_*). Earlier migration source under
            // database/raw/phase5/30-structure-measurements-visual.sql is
            // stale relative to the live table.
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
        } catch (\Throwable $e) { /* fallback empty */
        }

        // silver.samples (commodity-grade samples) — feeds the
        // CommoditySamples3DView sub-view. For Cameco this is the only
        // place uranium grade (U3O8_pct_e) appears at hole+depth resolution;
        // gold.assay_composites covers geochemistry/REE/base-metals but not
        // U. Available commodities are surfaced as a picker; default to the
        // one with the most non-null samples.
        $commoditySamples = [];
        $commodityKeys = [];
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
        } catch (\Throwable $e) { /* fallback empty */
        }

        // Project layer row counts — drives the Layers panel left rail. Each
        // entry has the layer label, the table it represents, and the row
        // count so the UI can dim layers with no data.
        $samplesCount = 0;
        $lithologyCount = 0;
        try {
            $lithologyCount = (int) DB::table('silver.lithology_logs as l')
                ->join('silver.collars as c', 'l.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $project->project_id)
                ->count();
        } catch (\Throwable $e) { /* fallback */
        }
        try {
            $samplesCount = (int) DB::table('silver.samples as s')
                ->join('silver.collars as c', 's.collar_id', '=', 'c.collar_id')
                ->where('c.project_id', $project->project_id)
                ->count();
        } catch (\Throwable $e) { /* fallback */
        }
        $savedViewsCount = 0;
        try {
            $savedViewsCount = (int) DB::table('silver.saved_map_views')
                ->where('project_id', $project->project_id)
                ->count();
        } catch (\Throwable $e) { /* fallback */
        }
        // Per-thickness tier counts — drives the "Ore tier ≥ Nm" toggles.
        $tierCounts = ['ore_5' => 0, 'ore_10' => 0, 'ore_20' => 0];
        try {
            foreach ([5, 10, 20] as $threshold) {
                $key = 'ore_'.$threshold;
                $tierCounts[$key] = (int) DB::table('gold.drillhole_intervals_visual')
                    ->where('project_id', $project->project_id)
                    ->where('lithology_code', 'DERIVED-ORE')
                    ->select('collar_id', DB::raw('SUM(depth_to - depth_from) AS thickness_m'))
                    ->groupBy('collar_id')
                    ->havingRaw('SUM(depth_to - depth_from) >= ?', [$threshold])
                    ->get()
                    ->count();
            }
        } catch (\Throwable $e) { /* fallback */
        }

        $aoiAvailable = $projectAoi !== null ? 1 : 0;
        $projectLayers = [
            ['id' => 'collars', 'label' => 'Collars', 'count' => $collars->count(), 'on' => true],
            ['id' => 'samples', 'label' => 'Ore-bearing holes only', 'count' => $oreHoleCount, 'on' => false],
            ['id' => 'ore_heatmap', 'label' => 'Ore heatmap', 'count' => $oreHoleCount, 'on' => false],
            ['id' => 'traces', 'label' => 'Drillhole traces', 'count' => $collars->count(), 'on' => false],
            ['id' => 'aoi', 'label' => 'Project AOI', 'count' => $aoiAvailable, 'on' => false],
            ['id' => 'tier_5', 'label' => 'Ore tier ≥ 5 m', 'count' => $tierCounts['ore_5'], 'on' => false],
            ['id' => 'tier_10', 'label' => 'Ore tier ≥ 10 m', 'count' => $tierCounts['ore_10'], 'on' => false],
            ['id' => 'tier_20', 'label' => 'Ore tier ≥ 20 m', 'count' => $tierCounts['ore_20'], 'on' => false],
            ['id' => 'lithology', 'label' => 'Lithology bands (logs only)', 'count' => $lithologyCount, 'on' => false],
            ['id' => 'sections', 'label' => 'Cross sections', 'count' => $sectionsCount, 'on' => false],
            ['id' => 'saved_views', 'label' => 'Saved views', 'count' => $savedViewsCount, 'on' => false],
        ];

        // Chronostratigraphic column. Prefer project-specific formations from
        // silver.geological_formations when present (any project). Fall back
        // to a regional reference column scoped to the project's jurisdiction
        // so the LOGS panel always has stratigraphic context next to the
        // multi-log curves.
        $stratUnits = [];
        $stratSource = 'reference';
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
        } catch (\Throwable $e) { /* fallback */
        }
        if (empty($stratUnits)) {
            $stratUnits = $this->referenceStratColumn($project);
        }

        // Public-geoscience layers are jurisdiction-scoped. Seeded PGEO data
        // (see 2026_05_13_180000_seed_public_geoscience_jurisdictions_and_sources.php)
        // is Canadian-only at the moment — 8 provinces (SK/ON/BC/QC/MB/AB/NS/NL).
        // For US projects (e.g. Wyoming Cameco Shirley Basin) we surface a
        // pending-state empty list rather than mis-label the Wyoming workspace
        // with Saskatchewan/Athabasca-flavoured GSC layers.
        $country = $this->resolveProjectCountry($project);

        if ($country === 'CA') {
            $publicGeoLayers = [
                ['id' => 'bedrock_geology', 'label' => 'Bedrock geology', 'tier' => 2, 'on' => false],
                ['id' => 'surficial_geology', 'label' => 'Surficial geology', 'tier' => 2, 'on' => false],
                ['id' => 'faults', 'label' => 'Faults', 'tier' => 2, 'on' => false],
                ['id' => 'dykes', 'label' => 'Dykes', 'tier' => 2, 'on' => false],
                ['id' => 'geological_domains', 'label' => 'Geological domains', 'tier' => 2, 'on' => false],
                ['id' => 'regional_compilation_points', 'label' => 'Regional compilation pts', 'tier' => 2, 'on' => false],
                ['id' => 'regional_compilation_polygons', 'label' => 'Regional compilation polys', 'tier' => 2, 'on' => false],
                ['id' => 'geoscience_publications', 'label' => 'Geoscience publications', 'tier' => 2, 'on' => false],
                ['id' => 'geophys_survey_coverage', 'label' => 'Geophys survey coverage', 'tier' => 2, 'on' => false],
                ['id' => 'geophys_control_points', 'label' => 'Geophys control points', 'tier' => 2, 'on' => false],
                ['id' => 'petroleum_wells', 'label' => 'Petroleum wells', 'tier' => 3, 'on' => false, 'locked' => true],
                ['id' => 'petroleum_well_trajectories', 'label' => 'Well trajectories', 'tier' => 3, 'on' => false, 'locked' => true],
                ['id' => 'petroleum_pools', 'label' => 'Petroleum pools', 'tier' => 3, 'on' => false, 'locked' => true],
                ['id' => 'geochronology_samples', 'label' => 'Geochronology samples', 'tier' => 3, 'on' => false, 'locked' => true],
                ['id' => 'geochemistry_samples', 'label' => 'Geochemistry samples', 'tier' => 3, 'on' => false, 'locked' => true],
                ['id' => 'geological_feature_points', 'label' => 'Feature points', 'tier' => 3, 'on' => false, 'locked' => true],
                ['id' => 'geological_feature_lines', 'label' => 'Feature lines', 'tier' => 3, 'on' => false, 'locked' => true],
            ];
            $pgeoNote = null;
        } else {
            // US (Wyoming) and any other non-Canadian jurisdiction: no public
            // geoscience corpus is seeded yet. WSGS ingest is a deferred
            // follow-up (see plan rev 6 §"Wyoming-specific deferred follow-ups").
            $publicGeoLayers = [];
            $pgeoNote = $country === 'US'
                ? 'No public geoscience layers seeded for this US jurisdiction yet. WSGS (Wyoming State Geological Survey) ingest is a deferred follow-up.'
                : 'No public geoscience layers available for this jurisdiction.';
        }

        return Inertia::render('Foundry/Workspace', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
                'company' => $project->company,
                'commodity' => $project->commodity,
                'region' => $project->region,
                'crs_epsg' => $project->crs_epsg,
            ],
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
            'log_hole_id' => $logHoleId,
            'log_depth_max' => $logDepthMax > 0 ? $logDepthMax : 600.0,
            'log_hole_options' => array_map(fn ($o) => $o['hole_id'], $logHoleOptions),
            'log_hole_total_depth' => $logHoleTotalDepth,
            'log_hole_easting' => $logHoleEasting,
            'log_hole_northing' => $logHoleNorthing,
            'log_lithology_intervals' => $logLithologyIntervals,
            'first_holes_intervals' => $firstHolesIntervals,
            // 3D mode payload — surveys feed MultiHole3DTrace; structures
            // feed the 3D Stereosphere. Both may be empty arrays.
            'surveys_3d' => $surveys,
            'structures_3d' => $structures,
            // Gold-tier sub-view payloads.
            'assay_composites_3d' => $assayComposites,
            'assay_elements_3d' => $assayElements,
            'significant_intersections_3d' => $significantIntersections,
            'structures_visual_3d' => $structuresVisual,
            'commodity_samples_3d' => $commoditySamples,
            'commodity_keys_3d' => $commodityKeys,
            'project_layers' => $projectLayers,
            'project_aoi' => $projectAoi,
            'strat_units' => $stratUnits,
            'strat_source' => $stratSource,
            'pgeo_layers' => $publicGeoLayers,
            'pgeo_note' => $pgeoNote,
            'pgeo_country' => $country,
            'empty' => $collars->isEmpty(),
        ]);
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

        $wanted = [
            ['curve' => 'GAMMA', 'label' => 'Gamma (cps)', 'color' => 'oklch(0.78 0.16 30)'],
            ['curve' => 'GRADE', 'label' => 'U grade (%eU₃O₈)', 'color' => 'oklch(0.82 0.18 145)'],
            ['curve' => 'RES', 'label' => 'Resistivity (Ω·m)', 'color' => 'oklch(0.72 0.14 220)'],
            ['curve' => 'SP', 'label' => 'SP (mV)', 'color' => 'oklch(0.65 0.10 280)'],
        ];

        $logTracks = [];
        $logDepthMax = 0.0;
        foreach ($wanted as $w) {
            $row = DB::table('silver.well_log_curves')
                ->where('collar_id', $collar->collar_id)
                ->where('curve_name', $w['curve'])
                ->select('depths', 'values', 'min_depth', 'max_depth', 'sample_count', 'null_value', 'curve_unit')
                ->first();
            if (! $row) {
                continue;
            }
            $depths = $this->parsePgDoubleArray($row->depths);
            $values = $this->parsePgDoubleArray($row->values);
            $n = min(count($depths), count($values));
            if ($n === 0) {
                continue;
            }
            $step = max(1, (int) floor($n / 240));
            $pts = [];
            $vmin = INF;
            $vmax = -INF;
            for ($i = 0; $i < $n; $i += $step) {
                $v = (float) $values[$i];
                if (abs($v - (float) $row->null_value) < 1e-6) {
                    continue;
                }
                $pts[] = ['depth' => (float) $depths[$i], 'value' => $v];
                $vmin = min($vmin, $v);
                $vmax = max($vmax, $v);
            }
            if (empty($pts)) {
                continue;
            }
            $logTracks[] = [
                'label' => $w['label'],
                'color' => $w['color'],
                'points' => $pts,
                'min' => is_finite($vmin) ? $vmin : 0,
                'max' => is_finite($vmax) ? $vmax : 1,
            ];
            $logDepthMax = max($logDepthMax, (float) $row->max_depth);
        }

        $lithologyIntervals = DB::table('gold.drillhole_intervals_visual')
            ->where('collar_id', $collar->collar_id)
            ->where('interval_kind', 'lithology')
            ->orderBy('depth_from')
            ->get(['depth_from', 'depth_to', 'lithology_code', 'lithology_label', 'color_hint'])
            ->map(fn ($r) => [
                'from' => (float) $r->depth_from,
                'to' => (float) $r->depth_to,
                'code' => (string) $r->lithology_code,
                'label' => (string) $r->lithology_label,
                'color' => (string) $r->color_hint,
            ])->values()->all();

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
            'log_depth_max' => $logDepthMax > 0 ? $logDepthMax : 600.0,
            'lithology_intervals' => $lithologyIntervals,
            'ore_bands' => (int) ($oreStats->n ?? 0),
            'ore_thickness_m' => round((float) ($oreStats->thickness ?? 0), 2),
            'mean_u3o8_pct' => $meanGrade && $meanGrade->mean_grade !== null
                ? round((float) $meanGrade->mean_grade, 5)
                : null,
        ]);
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
                    // 90 = horizontal. The trajectory integrator expects
                    // `dip` in the "angle from horizontal" convention used
                    // by MultiHole3DTrace / OrientationSpiral, where 90 =
                    // vertical down. Convert: dip = 90 - SANG.
                    'dip' => 90.0 - $d,
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
     * purpose of scoping public-geoscience layers.
     *
     * Today silver.projects has no jurisdiction/country column, so we fall
     * back to name-based detection. Wyoming Cameco Shirley Basin → 'US'.
     * Saskatchewan/Ontario/BC/... project names → 'CA'. Anything else
     * defaults to 'CA' to preserve existing behavior for the seeded
     * Canadian PGEO corpus.
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
