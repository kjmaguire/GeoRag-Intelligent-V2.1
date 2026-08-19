<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1\PublicGeoscience;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

/**
 * GeoJSON feed for the "Public Geoscience" map overlay — GET /api/v1/public-geoscience/map.
 *
 * 2026-08-19 — REWRITTEN for real data volume. The previous version was built
 * against the assumption, stated in its own docblock, of "~29 rows total
 * across all public_geo tables": it selected every row in the table, capped
 * at MAX_ROWS_PER_TABLE = 2000, and returned them as one unbounded GeoJSON
 * body with no viewport filter.
 *
 * That assumption was wrong by four orders of magnitude. The real corpus is
 * ~514k rows — pg_mineral_occurrence alone holds 412,537 (CA-BC 406,525 +
 * CA-SK 6,012). Against that data the old endpoint would have returned the
 * first 2,000 rows Postgres happened to hand back, silently, with no
 * indication that 99.5% of the layer was missing — a wrong map that looks
 * like a working one. That is the specific failure this rewrite exists to
 * prevent, so note the two invariants below:
 *
 *   1. Nothing is ever silently dropped. Every response carries
 *      `total_in_view` (the true COUNT for the query, independent of what
 *      was returned) and a `truncated` boolean. If the caller gets fewer
 *      features than exist, the payload says so and the UI is expected to
 *      surface it.
 *   2. Volume is handled by AGGREGATING, not by truncating. When a viewport
 *      holds more points than can be usefully drawn, the response switches
 *      to grid-aggregated cluster features covering ALL of them, rather
 *      than an arbitrary subset of individual points.
 *
 * Mode is chosen by actual count, not by a zoom threshold. Zoom only sets
 * the aggregation grid size. This matters because point density is wildly
 * uneven — zoom 7 over the BC interior is hundreds of thousands of
 * occurrences, zoom 7 over Nunavut is a handful — so a fixed zoom cutoff
 * would over-aggregate sparse regions and under-aggregate dense ones.
 *
 * Query parameters (all optional):
 *   bbox=minLng,minLat,maxLng,maxLat  viewport; defaults to whole world
 *   zoom=N                            0–22, sets cluster grid size; default 4
 *   jurisdiction=CA-BC                filter to one jurisdiction_code
 *
 * Scope: the 4 POINT-geometry public_geo tables — pg_mine,
 * pg_mineral_occurrence, pg_drillhole_collar, pg_rock_sample. The remaining
 * 4 tables (pg_resource_potential_zone, pg_assessment_survey,
 * pg_bedrock_geology, pg_mineral_disposition) are MULTIPOLYGON and remain
 * out of scope — rendering polygon overlays well (fill styling,
 * zoom-dependent simplification) is a different UI problem than point
 * markers. That is a disclosed boundary, not an oversight. Note it is now
 * a much larger exclusion than it was when first written: those four hold
 * ~65k rows between them, including 30,906 mineral dispositions.
 *
 * Every geom column is SRID 4326 POINT and carries a GiST index (verified
 * 2026-08-19), so the && bbox predicate below is index-assisted; without
 * those indexes this design would be far slower than the naive one.
 *
 * Not workspace/RLS-scoped — public_geo data isn't tenant data, same as
 * EntityReferencesController (its sibling in this namespace).
 *
 * NOTE ON EMPTY RESULTS: as of 2026-08-19 the Azure database has the
 * public_geo SCHEMA but zero rows in every data table — the migration
 * carried structure and not content. An empty FeatureCollection in
 * production is that gap, not a bug in this controller. The full dataset
 * lives in local Docker Postgres.
 */
class PublicGeoscienceMapController extends Controller
{
    /**
     * Per-layer ceiling on individual point features. Above this the layer
     * switches to cluster mode instead of truncating. Sized for what
     * MapLibre draws smoothly as an ordinary circle layer without
     * client-side clustering.
     */
    private const MAX_POINTS_PER_LAYER = 4000;

    /**
     * Per-layer ceiling on cluster cells. A grid fine enough to exceed this
     * within one viewport is finer than the screen can distinguish anyway.
     * If it is ever hit, `truncated` is set — clusters are not exempt from
     * invariant 1.
     */
    private const MAX_CLUSTERS_PER_LAYER = 2000;

    /**
     * The four point layers: table => [layer name, label column].
     */
    private const POINT_LAYERS = [
        'pg_mine' => ['mine', 'name'],
        'pg_mineral_occurrence' => ['mineral_occurrence', 'name'],
        'pg_drillhole_collar' => ['drillhole_collar', 'drillhole_name'],
        'pg_rock_sample' => ['rock_sample', 'station'],
    ];

    public function index(Request $request): JsonResponse
    {
        $validated = $request->validate([
            'bbox' => ['nullable', 'string', 'regex:/^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}$/'],
            'zoom' => ['nullable', 'numeric', 'between:0,22'],
            'jurisdiction' => ['nullable', 'string', 'max:16'],
        ]);

        $bbox = $this->parseBbox($validated['bbox'] ?? null);
        $zoom = (float) ($validated['zoom'] ?? 4);
        $jurisdiction = $validated['jurisdiction'] ?? null;

        $features = [];
        $totalInView = 0;
        $truncated = false;
        $modes = [];

        foreach (self::POINT_LAYERS as $table => [$layer, $labelColumn]) {
            $count = $this->countInView($table, $bbox, $jurisdiction);
            $totalInView += $count;

            if ($count === 0) {
                continue;
            }

            if ($count > self::MAX_POINTS_PER_LAYER) {
                $cells = $this->clusterFeatures($table, $layer, $bbox, $jurisdiction, $zoom);
                $features = [...$features, ...$cells];
                $modes[$layer] = 'clustered';
                // Invariant 1: a clipped cluster set is still a clipped
                // answer, even though the point total it covers is exact.
                if (count($cells) >= self::MAX_CLUSTERS_PER_LAYER) {
                    $truncated = true;
                }

                continue;
            }

            $points = $this->pointFeatures($table, $layer, $labelColumn, $bbox, $jurisdiction);
            $features = [...$features, ...$points];
            $modes[$layer] = 'points';
        }

        return response()->json([
            'type' => 'FeatureCollection',
            // True count of underlying records matching the query, whatever
            // mode each layer resolved to. The UI reads this — never
            // count(features) — when telling the user how much is out there.
            'total_in_view' => $totalInView,
            'feature_count' => count($features),
            'truncated' => $truncated,
            'zoom' => $zoom,
            'modes' => $modes,
            'features' => $features,
        ]);
    }

    /**
     * @return array{0: float, 1: float, 2: float, 3: float}
     */
    private function parseBbox(?string $raw): array
    {
        if ($raw === null) {
            return [-180.0, -90.0, 180.0, 90.0];
        }

        [$minLng, $minLat, $maxLng, $maxLat] = array_map(
            static fn (string $v): float => (float) $v,
            explode(',', $raw),
        );

        // Tolerate a viewport handed over in either corner order, and clamp
        // to valid lng/lat. MapLibre can report a bbox wider than the world
        // when zoomed out past a full rotation; ST_MakeEnvelope would then
        // build an envelope no row matches.
        return [
            max(-180.0, min($minLng, $maxLng)),
            max(-90.0, min($minLat, $maxLat)),
            min(180.0, max($minLng, $maxLng)),
            min(90.0, max($minLat, $maxLat)),
        ];
    }

    /**
     * Cluster grid size in degrees for a zoom level.
     *
     * 360° / 2^(zoom+3) puts roughly 8 cells across the viewport's width at
     * any zoom, which reads as clusters rather than as a grid pattern.
     * Clamped so a hostile or absurd zoom can't request a grid so fine that
     * the GROUP BY degenerates into one cell per row.
     */
    private function gridSize(float $zoom): float
    {
        return max(0.0005, 360.0 / (2 ** ($zoom + 3)));
    }

    private function countInView(string $table, array $bbox, ?string $jurisdiction): int
    {
        $query = DB::table("public_geo.{$table}")
            ->whereNotNull('geom')
            ->whereRaw('geom && ST_MakeEnvelope(?, ?, ?, ?, 4326)', $bbox);

        if ($jurisdiction !== null) {
            $query->where('jurisdiction_code', $jurisdiction);
        }

        return $query->count();
    }

    /**
     * @return list<array<string, mixed>>
     */
    private function pointFeatures(
        string $table,
        string $layer,
        string $labelColumn,
        array $bbox,
        ?string $jurisdiction,
    ): array {
        $query = DB::table("public_geo.{$table}")
            ->whereNotNull('geom')
            ->whereRaw('geom && ST_MakeEnvelope(?, ?, ?, ?, 4326)', $bbox)
            ->selectRaw(
                "id, jurisdiction_code, source_id, {$labelColumn} AS label, ".
                'ST_X(geom) AS lng, ST_Y(geom) AS lat',
            )
            ->limit(self::MAX_POINTS_PER_LAYER);

        if ($jurisdiction !== null) {
            $query->where('jurisdiction_code', $jurisdiction);
        }

        return $query->get()->map(fn ($r) => [
            'type' => 'Feature',
            'geometry' => [
                'type' => 'Point',
                'coordinates' => [(float) $r->lng, (float) $r->lat],
            ],
            'properties' => [
                'id' => (string) $r->id,
                'layer' => $layer,
                'cluster' => false,
                'label' => $r->label !== null ? (string) $r->label : null,
                'jurisdiction_code' => (string) $r->jurisdiction_code,
                'source_id' => (string) $r->source_id,
            ],
        ])->values()->all();
    }

    /**
     * Grid-aggregated cluster cells covering every point in the viewport.
     *
     * ST_SnapToGrid buckets by rounded coordinate; the returned position is
     * the centroid of each bucket's members rather than the grid node, so
     * clusters sit on their data instead of on a lattice.
     *
     * @return list<array<string, mixed>>
     */
    private function clusterFeatures(
        string $table,
        string $layer,
        array $bbox,
        ?string $jurisdiction,
        float $zoom,
    ): array {
        $grid = $this->gridSize($zoom);

        $query = DB::table("public_geo.{$table}")
            ->whereNotNull('geom')
            ->whereRaw('geom && ST_MakeEnvelope(?, ?, ?, ?, 4326)', $bbox)
            ->selectRaw(
                'COUNT(*) AS n, '.
                'ST_X(ST_Centroid(ST_Collect(geom))) AS lng, '.
                'ST_Y(ST_Centroid(ST_Collect(geom))) AS lat',
            )
            ->groupByRaw('ST_SnapToGrid(geom, ?, ?)', [$grid, $grid])
            ->orderByRaw('COUNT(*) DESC')
            ->limit(self::MAX_CLUSTERS_PER_LAYER);

        if ($jurisdiction !== null) {
            $query->where('jurisdiction_code', $jurisdiction);
        }

        return $query->get()->map(fn ($r) => [
            'type' => 'Feature',
            'geometry' => [
                'type' => 'Point',
                'coordinates' => [(float) $r->lng, (float) $r->lat],
            ],
            'properties' => [
                'layer' => $layer,
                'cluster' => true,
                'point_count' => (int) $r->n,
            ],
        ])->values()->all();
    }
}
