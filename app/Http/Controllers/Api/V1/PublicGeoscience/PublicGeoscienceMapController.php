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
 * 2026-08-17 — this is a REBUILD, not a restore. The original standalone
 * Public Geoscience browsing surface (PublicGeoscienceMap.tsx + a 620-line
 * Martin vector-tile proxy, TileProxyController.php) was removed on
 * 2026-07-27, and the Martin tile server itself was deleted in the SAME
 * commit that removed the proxy — so restoring the old frontend verbatim
 * would render a blank basemap with no working data source at all. See
 * this session's plan addendum for the full history.
 *
 * Rather than stand up a new Martin deployment + MVT pipeline for a
 * dataset this small (~29 rows total across all public_geo tables as of
 * the 2026-05-16 schema audit — Dagster's public_geoscience_weekly_refresh
 * schedule that would populate real volume is `default_status=STOPPED`,
 * and Dagster itself is dormant), this returns plain GeoJSON and the
 * frontend renders it as an ordinary MapLibre source/layer — the same
 * pattern MapView.tsx already uses for the coverage-density layer
 * (CoverageDensityController). No new infrastructure, no tile pipeline.
 *
 * Scope: the 4 POINT-geometry public_geo tables — pg_mine,
 * pg_mineral_occurrence, pg_drillhole_collar, pg_rock_sample. The
 * remaining 4 tables (pg_resource_potential_zone, pg_assessment_survey,
 * pg_bedrock_geology, pg_mineral_disposition) are MULTIPOLYGON and are
 * deliberately NOT included in this pass — rendering polygon overlays
 * well (fill styling, zoom-dependent simplification) is a meaningfully
 * different UI problem than point markers, and every citation-drill-in
 * consumer of public_geo data (EntityReferencesController) already only
 * covers point-shaped entities plus resource_potential_zone specifically,
 * not the other three polygon tables — so this isn't a regression against
 * anything that worked before. Not silently dropped: this is the disclosed
 * scope, not an oversight.
 *
 * Not workspace/RLS-scoped — public_geo data isn't tenant data, same as
 * EntityReferencesController (its sibling in this namespace) doesn't use
 * SetsWorkspaceRlsContext either.
 */
class PublicGeoscienceMapController extends Controller
{
    /**
     * Hard cap per table — defensive only. Real volume today is a few
     * dozen rows total; this exists so a future real Dagster backfill
     * (see class docblock) can't silently turn this into an unbounded
     * multi-thousand-feature payload without someone noticing the cap
     * kicking in and revisiting this controller.
     */
    private const MAX_ROWS_PER_TABLE = 2000;

    public function index(Request $request): JsonResponse
    {
        $jurisdiction = $request->query('jurisdiction');

        $features = [
            ...$this->pointFeatures('pg_mine', 'mine', 'name', $jurisdiction),
            ...$this->pointFeatures('pg_mineral_occurrence', 'mineral_occurrence', 'name', $jurisdiction),
            ...$this->pointFeatures('pg_drillhole_collar', 'drillhole_collar', 'drillhole_name', $jurisdiction),
            ...$this->pointFeatures('pg_rock_sample', 'rock_sample', 'station', $jurisdiction),
        ];

        return response()->json([
            'type' => 'FeatureCollection',
            'feature_count' => count($features),
            'features' => $features,
        ]);
    }

    /**
     * @return list<array<string, mixed>>
     */
    private function pointFeatures(string $table, string $layer, string $nameColumn, ?string $jurisdiction): array
    {
        $query = DB::table("public_geo.{$table}")
            ->whereNotNull('geom')
            ->selectRaw(
                "id, jurisdiction_code, source_id, {$nameColumn} AS label, ".
                'ST_X(geom) AS lng, ST_Y(geom) AS lat',
            )
            ->limit(self::MAX_ROWS_PER_TABLE);

        if ($jurisdiction) {
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
                'label' => $r->label !== null ? (string) $r->label : null,
                'jurisdiction_code' => (string) $r->jurisdiction_code,
                'source_id' => (string) $r->source_id,
            ],
        ])->values()->all();
    }
}
