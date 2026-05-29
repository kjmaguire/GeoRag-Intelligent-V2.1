<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Serves the /public-geoscience Inertia surface (Phase 1 scaffold).
 *
 * The page fetches its jurisdictions/sources data client-side through the API
 * (via useDashboardFetch → /api/v1/public-geoscience/jurisdictions), matching
 * the Dashboard Portfolio pattern — so no Inertia props are passed here.
 *
 * Phase 4 — exception: `pgeo_jurisdiction_epoch` seeds the
 * PublicGeoscienceMap tile cache-bust (`?v={epoch}` query suffix) on
 * initial render. The same epoch value drives the TileProxyController's
 * server-side ETag, so client cache-bust and server ETag stay in lockstep.
 * After mount, the `usePublicGeoscienceTileInvalidation` hook updates the
 * value live from Reverb broadcasts.
 */
class PublicGeoscienceController extends Controller
{
    /**
     * Same cache key + TTL as TileProxyController::computePgeoEtag.
     * Re-uses Laravel's cache fill semantics so the cold-miss query
     * (MAX aggregate on public_geo.jurisdictions) runs at most once per
     * 60-second window across all callers.
     */
    private const PGEO_EPOCH_CACHE_KEY = 'pgeo_jurisdiction_epoch';

    private const PGEO_EPOCH_CACHE_TTL = 60;

    public function index(): Response
    {
        return Inertia::render('PublicGeoscience/Index', [
            'pgeo_jurisdiction_epoch' => $this->currentJurisdictionEpoch(),
        ]);
    }

    /**
     * Read the current MAX(updated_at) epoch_s from public_geo.jurisdictions,
     * 60 s Redis cache. Matches TileProxyController's derivation exactly.
     *
     * Returns 0 when the table is empty or absent — the client-side
     * setTiles cache-bust still works, the URL just has ?v=0 until the
     * next invalidation event lands.
     */
    private function currentJurisdictionEpoch(): int
    {
        return (int) Cache::remember(
            self::PGEO_EPOCH_CACHE_KEY,
            self::PGEO_EPOCH_CACHE_TTL,
            static function (): int {
                try {
                    $row = DB::selectOne(
                        'SELECT EXTRACT(EPOCH FROM MAX(updated_at))::bigint AS epoch_s
                         FROM public_geo.jurisdictions',
                    );

                    return (int) ($row?->epoch_s ?? 0);
                } catch (\Throwable) {
                    // Schema may not exist in some test envs; degrade silently.
                    return 0;
                }
            },
        );
    }
}
