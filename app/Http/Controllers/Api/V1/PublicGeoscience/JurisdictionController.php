<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1\PublicGeoscience;

use App\Http\Controllers\Controller;
use App\Http\Resources\PublicGeoscience\JurisdictionResource;
use App\Models\PublicGeoscience\Jurisdiction;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Cache;

/**
 * Read-only jurisdiction registry endpoint for the Public Geoscience surface.
 *
 * Contract intent per plan §10:
 *   GET /api/v1/public-geoscience/jurisdictions → grouped by country_code
 *       with nested sources.
 *
 * Result is wrapped in the same `{ data, generated_at, cache_ttl_seconds }`
 * envelope used by the dashboard endpoints so the React client can reuse
 * the existing `useDashboardFetch` hook.
 *
 * Cache: 5-minute in-memory/Redis cache via the app's default store. Cheap
 * to recompute; cache exists mainly to keep Octane-warm responses instant
 * during page navigations.
 */
class JurisdictionController extends Controller
{
    private const CACHE_KEY = 'public-geoscience:jurisdictions:v1';

    private const CACHE_TTL_SECONDS = 300;

    public function index(Request $request): JsonResponse
    {
        $payload = Cache::remember(
            self::CACHE_KEY,
            self::CACHE_TTL_SECONDS,
            fn () => $this->build(),
        );

        return response()->json([
            'data' => $payload,
            'generated_at' => now()->toIso8601String(),
            'cache_ttl_seconds' => self::CACHE_TTL_SECONDS,
        ]);
    }

    /**
     * Build the payload: all jurisdictions ordered by sort_order, grouped by
     * country_code, with nested source rows.
     *
     * PostGIS bbox is converted to GeoJSON via ST_AsGeoJSON and hydrated onto
     * each model as `bbox_geojson` for the Resource to decode.
     */
    private function build(): array
    {
        $jurisdictions = Jurisdiction::query()
            ->with(['sources' => fn ($q) => $q->orderBy('canonical_type')])
            ->select('public_geo.jurisdictions.*')
            ->selectRaw('ST_AsGeoJSON(bbox) AS bbox_geojson')
            ->orderBy('sort_order')
            ->get();

        // Group by country_code. For Canada the only non-'coming_soon' entry
        // in Phase 1 is CA-SK; all others render as muted tiles.
        $byCountry = $jurisdictions
            ->groupBy('country_code')
            ->map(fn ($rows, $cc) => [
                'country_code' => $cc,
                'display_name' => $this->countryDisplayName($cc),
                'jurisdictions' => JurisdictionResource::collection($rows)
                    ->resolve(),
            ])
            ->values()
            ->all();

        return [
            'countries' => $byCountry,
            'counts' => [
                'total' => $jurisdictions->count(),
                'active' => $jurisdictions->where('status', 'active')->count(),
                'coming_soon' => $jurisdictions->where('status', 'coming_soon')->count(),
            ],
        ];
    }

    /**
     * Human-readable country label. Keeping this inline for Phase 1 — when a
     * second country is added we'll promote to a `countries` registry table
     * (plan §02c).
     */
    private function countryDisplayName(string $code): string
    {
        return match ($code) {
            'CA' => 'Canada',
            default => $code,
        };
    }
}
