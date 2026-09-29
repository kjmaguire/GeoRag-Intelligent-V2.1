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
 *   layers=mineral_disposition,…      polygon layers to include (see below);
 *                                     omitted = none, i.e. the old response
 *
 * Point scope: the 4 POINT-geometry public_geo tables — pg_mine,
 * pg_mineral_occurrence, pg_drillhole_collar, pg_rock_sample — always
 * returned, exactly as before, in `features`.
 *
 * Polygon scope (2026-09-29; previously excluded as "a different UI problem"):
 * the 4 MULTIPOLYGON tables — pg_mineral_disposition (tenure),
 * pg_resource_potential_zone, pg_assessment_survey, pg_bedrock_geology — are
 * returned ONLY when named in `layers=`, in a separate `polygons`
 * FeatureCollection so no point-layer filter can ever match a polygon. Volume
 * is bounded three ways, because a province-wide bbox over ~65k polygons
 * (30,906 SK dispositions alone) must not return megabytes:
 *
 *   1. a per-layer minimum zoom below which nothing is fetched (mode
 *      'min_zoom' — the UI says "zoom in"), since 30k parcels at zoom 4 are
 *      unreadable anyway;
 *   2. geometry is clipped to the (slightly padded) viewport and simplified
 *      with ST_SimplifyPreserveTopology at a tolerance of ~half a screen
 *      pixel for the requested zoom, then emitted at 6 decimal places;
 *   3. a hard cap of MAX_POLYGONS_PER_LAYER features (largest first) and a
 *      MAX_POLYGON_BYTES budget across all polygon layers.
 *
 * Invariant 1 below holds for polygons too: `polygon_layers.<layer>` carries
 * the true `total_in_view` and a `truncated` flag whenever a cap bit.
 * `sources` maps each returned source_id to its name and licence, which the
 * popups show — these are government open-data licences that require
 * attribution.
 *
 * Every geom column is SRID 4326 and carries a GiST index (points verified
 * 2026-08-19; the polygon tables' idx_*_geom GiST indexes are created by
 * their own migrations), so the && bbox predicate below is index-assisted;
 * without those indexes this design would be far slower than the naive one.
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

    /**
     * Per-layer ceiling on polygon features, largest first.
     */
    public const MAX_POLYGONS_PER_LAYER = 1500;

    /**
     * Budget for the serialised polygon geometry across ALL polygon layers
     * in one response. Once spent, remaining features are dropped and the
     * layer is marked truncated.
     */
    public const MAX_POLYGON_BYTES = 4_000_000;

    /**
     * The polygon layers: layer name => table, minimum zoom, label SQL, and
     * the key attributes (alias => SQL expression) a popup shows. Every
     * expression is a fixed string from this constant, never user input.
     *
     * @var array<string, array{table: string, min_zoom: float, label: string, attrs: array<string, string>}>
     */
    public const POLYGON_LAYERS = [
        'mineral_disposition' => [
            'table' => 'pg_mineral_disposition',
            'min_zoom' => 6.0,
            'label' => 'disposition_number',
            'attrs' => [
                'disposition_type' => 'disposition_type',
                'status' => 'status',
                'holder_name' => 'holder_name',
                'issue_date' => 'issue_date::text',
                'expiry_date' => 'expiry_date::text',
                'area_ha' => 'area_ha::text',
            ],
        ],
        'resource_potential_zone' => [
            'table' => 'pg_resource_potential_zone',
            'min_zoom' => 3.0,
            'label' => 'commodity',
            'attrs' => [
                'commodity' => 'commodity',
                'potential_rank' => 'potential_rank::text',
                'methodology_ref' => 'methodology_ref',
            ],
        ],
        'assessment_survey' => [
            'table' => 'pg_assessment_survey',
            'min_zoom' => 6.0,
            'label' => "source_attributes->>'FILENUMBER'",
            'attrs' => [
                'survey_type' => 'survey_type',
                'file_number' => "source_attributes->>'FILENUMBER'",
                'company' => "source_attributes->>'COMPANY'",
            ],
        ],
        'bedrock_geology' => [
            'table' => 'pg_bedrock_geology',
            'min_zoom' => 5.0,
            'label' => 'COALESCE(unit_name, unit_code)',
            'attrs' => [
                'unit_code' => 'unit_code',
                'unit_name' => 'unit_name',
                'period' => 'period',
                'group_name' => 'group_name',
                'formation' => 'formation',
                'lithology' => 'lithology',
                'scale' => 'scale',
            ],
        ],
    ];

    public function index(Request $request): JsonResponse
    {
        $validated = $request->validate([
            'bbox' => ['nullable', 'string', 'regex:/^-?\d+(\.\d+)?(,-?\d+(\.\d+)?){3}$/'],
            'zoom' => ['nullable', 'numeric', 'between:0,22'],
            'jurisdiction' => ['nullable', 'string', 'max:16'],
            'layers' => [
                'nullable', 'string', 'max:200',
                function (string $attribute, mixed $value, \Closure $fail): void {
                    foreach (explode(',', (string) $value) as $layer) {
                        if (! array_key_exists(trim($layer), self::POLYGON_LAYERS)) {
                            $fail("Unknown polygon layer '{$layer}'. Allowed: "
                                .implode(', ', array_keys(self::POLYGON_LAYERS)).'.');
                        }
                    }
                },
            ],
        ]);

        $bbox = $this->parseBbox($validated['bbox'] ?? null);
        $zoom = (float) ($validated['zoom'] ?? 4);
        $jurisdiction = $validated['jurisdiction'] ?? null;
        $polygonLayers = array_values(array_unique(array_filter(array_map(
            'trim',
            explode(',', (string) ($validated['layers'] ?? '')),
        ))));

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

        $payload = [
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
        ];

        if ($polygonLayers !== []) {
            [$polygons, $polygonMeta, $sources] = $this->polygonFeatures($polygonLayers, $bbox, $zoom, $jurisdiction);
            $payload['polygons'] = ['type' => 'FeatureCollection', 'features' => $polygons];
            $payload['polygon_layers'] = $polygonMeta;
            $payload['sources'] = $sources;
        }

        return response()->json($payload);
    }

    /**
     * Simplification tolerance in degrees: about half a screen pixel at this
     * zoom (a 256 px tile spans 360° / 2^zoom). Finer than that is invisible;
     * coarser starts to visibly move boundaries.
     */
    public static function simplifyTolerance(float $zoom): float
    {
        return max(0.000001, 360.0 / (256 * (2 ** $zoom)) * 0.5);
    }

    /**
     * Requested polygon layers, bounded per the class docblock.
     *
     * @param list<string> $layers
     * @param array{0: float, 1: float, 2: float, 3: float} $bbox
     *
     * @return array{0: list<array<string, mixed>>, 1: array<string, array<string, mixed>>, 2: array<string, array<string, ?string>>}
     */
    private function polygonFeatures(array $layers, array $bbox, float $zoom, ?string $jurisdiction): array
    {
        $features = [];
        $meta = [];
        $sourceIds = [];
        $bytesLeft = self::MAX_POLYGON_BYTES;
        $tolerance = self::simplifyTolerance($zoom);

        // Clip to the viewport padded by 10% so a clipped edge never shows
        // on screen as a fake boundary.
        [$minLng, $minLat, $maxLng, $maxLat] = $bbox;
        $padLng = ($maxLng - $minLng) * 0.1;
        $padLat = ($maxLat - $minLat) * 0.1;
        $clip = [
            max(-180.0, $minLng - $padLng), max(-90.0, $minLat - $padLat),
            min(180.0, $maxLng + $padLng), min(90.0, $maxLat + $padLat),
        ];

        foreach ($layers as $layer) {
            $def = self::POLYGON_LAYERS[$layer];

            if ($zoom < $def['min_zoom']) {
                $meta[$layer] = [
                    'mode' => 'min_zoom',
                    'min_zoom' => $def['min_zoom'],
                    'total_in_view' => null,
                    'returned' => 0,
                    'truncated' => false,
                ];

                continue;
            }

            $total = $this->countInView($def['table'], $bbox, $jurisdiction);
            $returned = 0;
            $clipped = false;

            if ($total > 0) {
                $attrSql = implode(', ', array_map(
                    static fn (string $alias, string $expr): string => "{$expr} AS \"{$alias}\"",
                    array_keys($def['attrs']),
                    array_values($def['attrs']),
                ));

                $query = DB::table("public_geo.{$def['table']}")
                    ->whereNotNull('geom')
                    ->whereRaw('geom && ST_MakeEnvelope(?, ?, ?, ?, 4326)', $bbox)
                    ->selectRaw(
                        "id, jurisdiction_code, source_id, {$def['label']} AS label, {$attrSql}, "
                        .'ST_AsGeoJSON(ST_SimplifyPreserveTopology('
                        .'ST_ClipByBox2D(geom, ST_MakeEnvelope(?, ?, ?, ?, 4326)), ?), 6) AS geojson',
                        [...$clip, $tolerance],
                    )
                    // Largest first: under the cap, the parcels that matter
                    // most at this zoom are the ones a user can actually see.
                    ->orderByRaw('ST_Area(geom) DESC')
                    ->limit(self::MAX_POLYGONS_PER_LAYER);

                if ($jurisdiction !== null) {
                    $query->where('jurisdiction_code', $jurisdiction);
                }

                foreach ($query->get() as $row) {
                    $json = (string) ($row->geojson ?? '');
                    if ($json === '') {
                        continue;
                    }
                    if (strlen($json) > $bytesLeft) {
                        $clipped = true;

                        break;
                    }
                    $geometry = json_decode($json, true);
                    if (! is_array($geometry) || ($geometry['coordinates'] ?? []) === []) {
                        continue; // simplified/clipped away entirely
                    }
                    $bytesLeft -= strlen($json);

                    $properties = [
                        'id' => (string) $row->id,
                        'layer' => $layer,
                        'label' => $row->label !== null ? (string) $row->label : null,
                        'jurisdiction_code' => (string) $row->jurisdiction_code,
                        'source_id' => (string) $row->source_id,
                    ];
                    foreach (array_keys($def['attrs']) as $alias) {
                        $properties[$alias] = $row->{$alias} !== null ? (string) $row->{$alias} : null;
                    }

                    $features[] = ['type' => 'Feature', 'geometry' => $geometry, 'properties' => $properties];
                    $sourceIds[(string) $row->source_id] = true;
                    $returned++;
                }
            }

            $meta[$layer] = [
                'mode' => 'polygons',
                'min_zoom' => $def['min_zoom'],
                'total_in_view' => $total,
                'returned' => $returned,
                // Invariant 1: say so whenever what came back is not all there is.
                'truncated' => $clipped || $returned < $total,
            ];
        }

        return [$features, $meta, $this->sourceAttribution(array_keys($sourceIds))];
    }

    /**
     * Name + licence for each returned source_id (popup attribution).
     *
     * @param list<string> $sourceIds
     *
     * @return array<string, array{name: ?string, license_summary: ?string, license_url: ?string}>
     */
    private function sourceAttribution(array $sourceIds): array
    {
        if ($sourceIds === []) {
            return [];
        }

        $out = [];
        foreach (DB::table('public_geo.sources')
            ->whereIn('source_id', $sourceIds)
            ->get(['source_id', 'name', 'license_summary', 'license_url']) as $s) {
            $out[(string) $s->source_id] = [
                'name' => $s->name !== null ? (string) $s->name : null,
                'license_summary' => $s->license_summary !== null ? (string) $s->license_summary : null,
                'license_url' => $s->license_url !== null ? (string) $s->license_url : null,
            ];
        }

        return $out;
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
