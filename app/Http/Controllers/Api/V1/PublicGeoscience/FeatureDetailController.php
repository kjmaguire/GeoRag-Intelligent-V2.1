<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1\PublicGeoscience;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Symfony\Component\HttpKernel\Exception\NotFoundHttpException;

/**
 * Read-only feature detail endpoint for the Public Geoscience surface.
 *
 *   GET /api/v1/public-geoscience/features/{layer}/{featureId}
 *       → full canonical row (incl. source_attributes JSONB and
 *         reserves_resources JSONB) for the upstream record behind a
 *         single MVT feature.
 *
 * Drives the in-map Expanded-Feature panel + the Compare-Features modal.
 * The MVT tile only carries a trimmed property set (to keep tiles cheap);
 * this endpoint is the on-demand source of truth for the FULL upstream
 * record, including the raw JSONB blob from the source agency.
 *
 * Cache: 60-second per-(layer, id) cache. Upstream PG data refreshes via
 * Dagster pulls on a multi-day cadence, but we keep the TTL short because
 * the cost of a missed cache hit is one PostGIS row read, while a longer
 * TTL would lag if a manual refresh ran during a session.
 */
class FeatureDetailController extends Controller
{
    private const CACHE_TTL_SECONDS = 60;

    /**
     * Per-layer table + identifier-column registry. Keys mirror the
     * MapLibre layer IDs the React client uses; values point at the
     * canonical singular tables (NOT the *_mvt views, which are tile-
     * trimmed). `smdi_deposits` lives outside public_geo because it's
     * an unconsolidated parallel stack (plan v1.1).
     *
     * @var array<string, array{table: string, id_column: string}>
     */
    private const LAYER_TABLES = [
        'pg_mines' => ['table' => 'public_geo.pg_mine',                    'id_column' => 'source_feature_id'],
        'pg_mineral_occurrences' => ['table' => 'public_geo.pg_mineral_occurrence',      'id_column' => 'source_feature_id'],
        'pg_drillhole_collars' => ['table' => 'public_geo.pg_drillhole_collar',        'id_column' => 'source_feature_id'],
        'pg_rock_samples' => ['table' => 'public_geo.pg_rock_sample',             'id_column' => 'source_feature_id'],
        'pg_resource_potential' => ['table' => 'public_geo.pg_resource_potential_zone', 'id_column' => 'source_feature_id'],
        'pg_assessment_surveys' => ['table' => 'public_geo.pg_assessment_survey',       'id_column' => 'source_feature_id'],
        'pg_mineral_dispositions' => ['table' => 'public_geo.pg_mineral_disposition',     'id_column' => 'source_feature_id'],
        'smdi_deposits' => ['table' => 'public.smdi_deposits',                  'id_column' => 'smdi'],
    ];

    public function show(string $layer, string $featureId): JsonResponse
    {
        if (! isset(self::LAYER_TABLES[$layer])) {
            throw new NotFoundHttpException("Unknown layer: {$layer}");
        }

        $cacheKey = "public-geoscience:feature:v1:{$layer}:{$featureId}";

        $payload = Cache::remember(
            $cacheKey,
            self::CACHE_TTL_SECONDS,
            fn () => $this->fetchRow($layer, $featureId),
        );

        if ($payload === null) {
            throw new NotFoundHttpException("Feature not found: {$layer}/{$featureId}");
        }

        return response()->json([
            'data' => $payload,
            'generated_at' => now()->toIso8601String(),
            'cache_ttl_seconds' => self::CACHE_TTL_SECONDS,
        ]);
    }

    /**
     * Read the row, decode JSONB columns into PHP arrays so the JSON
     * response stays a single decode on the client (no nested
     * stringified blobs). Returns null when no row matches the id.
     *
     * @return array<string, mixed>|null
     */
    private function fetchRow(string $layer, string $featureId): ?array
    {
        ['table' => $table, 'id_column' => $idColumn] = self::LAYER_TABLES[$layer];

        $row = DB::table($table)
            ->where($idColumn, $featureId)
            ->first();

        if ($row === null) {
            return null;
        }

        $arr = (array) $row;

        // Drop columns the client never needs and which inflate the
        // payload (raw geom is large, checksum is internal, source_geom_wkt
        // duplicates geom).
        foreach (['geom', 'source_geom_wkt', 'checksum'] as $drop) {
            unset($arr[$drop]);
        }

        // Decode JSONB columns. PG-side they're text; consumer wants
        // already-parsed objects so it can render key/value pairs without
        // a second JSON.parse.
        foreach (['source_attributes', 'reserves_resources'] as $jsonCol) {
            if (isset($arr[$jsonCol]) && is_string($arr[$jsonCol])) {
                $decoded = json_decode($arr[$jsonCol], true);
                $arr[$jsonCol] = is_array($decoded) ? $decoded : null;
            }
        }

        $arr['layer'] = $layer;

        return $arr;
    }
}
