<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1\PublicGeoscience;

use App\Http\Controllers\Controller;
use App\Services\PublicGeoSyncTrigger;
use App\Services\PublicGeoSyncTriggerException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;

/**
 * Operator controls for the public-geo mirror on the Public Geo page.
 *
 *   POST /api/v1/public-geoscience/sync         admin only — "Sync now"
 *   GET  /api/v1/public-geoscience/sync-status  any signed-in user — freshness
 *
 * "Sync now" enqueues the `public_geo_sync` Hatchet workflow through FastAPI
 * (PublicGeoSyncTrigger) and answers 202 with the workflow run id. Before it
 * existed, a refresh outside the Sunday cron meant an `aws ecs run-task` from
 * CloudShell.
 *
 * Double-trigger guard, two layers:
 *   1. Here, a short cooldown (Cache::add, atomic on the Redis store): a
 *      second click within COOLDOWN_SECONDS gets 429 with the run id the
 *      first click started, instead of a second dispatch.
 *   2. In Hatchet, the workflow's single-flight concurrency group
 *      (CANCEL_NEWEST): a run dispatched while another is still in flight —
 *      the cron, or a trigger from after the cooldown — is cancelled by the
 *      engine. That is the real guard; the cooldown only spares the queue.
 *
 * sync-status reports row counts and the latest `last_seen_at` per layer and
 * jurisdiction — `last_seen_at` is stamped on every row a sync observes, so
 * its maximum IS the last successful sync time for that feed family. Cached
 * briefly: it counts ~500k rows.
 */
class PublicGeoscienceSyncController extends Controller
{
    public const COOLDOWN_SECONDS = 120;

    public const COOLDOWN_KEY = 'public-geo:sync:last-trigger';

    private const STATUS_CACHE_KEY = 'public-geo:sync-status';

    private const STATUS_CACHE_SECONDS = 300;

    /**
     * Every public_geo table the sync writes: table => layer name.
     */
    private const TABLES = [
        'pg_mine' => 'mine',
        'pg_mineral_occurrence' => 'mineral_occurrence',
        'pg_drillhole_collar' => 'drillhole_collar',
        'pg_rock_sample' => 'rock_sample',
        'pg_mineral_disposition' => 'mineral_disposition',
        'pg_resource_potential_zone' => 'resource_potential_zone',
        'pg_assessment_survey' => 'assessment_survey',
        'pg_bedrock_geology' => 'bedrock_geology',
    ];

    public function store(Request $request, PublicGeoSyncTrigger $trigger): JsonResponse
    {
        $this->authorize('admin');

        $validated = $request->validate([
            'jurisdiction_codes' => ['nullable', 'array', 'max:20'],
            'jurisdiction_codes.*' => ['string', 'regex:/^[A-Z]{2}-[A-Z]{2,10}$/'],
        ]);

        /** @var list<string> $codes */
        $codes = array_values(array_unique($validated['jurisdiction_codes'] ?? []));
        $user = $request->user();
        abort_if($user === null, 401);

        $claim = ['workflow_run_id' => null, 'triggered_at' => now()->toIso8601String(), 'by' => $user->email];
        if (! Cache::add(self::COOLDOWN_KEY, $claim, self::COOLDOWN_SECONDS)) {
            /** @var array{workflow_run_id: ?string, triggered_at: string, by: ?string}|null $previous */
            $previous = Cache::get(self::COOLDOWN_KEY);

            return response()->json([
                'error' => 'sync_recently_triggered',
                'message' => 'A public-geo sync was triggered less than '
                    .intdiv(self::COOLDOWN_SECONDS, 60).' minutes ago.',
                'workflow_run_id' => $previous['workflow_run_id'] ?? null,
                'triggered_at' => $previous['triggered_at'] ?? null,
            ], 429);
        }

        try {
            $result = $trigger->trigger(
                $codes === [] ? null : $codes,
                $user->getKey(),
                'web:'.$user->email,
            );
        } catch (PublicGeoSyncTriggerException $exc) {
            // Nothing was dispatched — release the cooldown so a retry works.
            Cache::forget(self::COOLDOWN_KEY);

            return response()->json(['error' => 'trigger_failed', 'message' => $exc->getMessage()], $exc->status);
        }

        Cache::put(
            self::COOLDOWN_KEY,
            [...$claim, 'workflow_run_id' => $result['workflow_run_id']],
            self::COOLDOWN_SECONDS,
        );

        return response()->json($result, 202);
    }

    public function status(): JsonResponse
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return response()->json(['layers' => [], 'last_seen_at' => null]);
        }

        /** @var list<array{layer: string, jurisdiction_code: string, rows: int, last_seen_at: ?string}> $layers */
        $layers = Cache::remember(self::STATUS_CACHE_KEY, self::STATUS_CACHE_SECONDS, function (): array {
            $selects = [];
            foreach (self::TABLES as $table => $layer) {
                $selects[] = "SELECT '{$layer}'::text AS layer, jurisdiction_code, COUNT(*) AS row_count, "
                    ."MAX(last_seen_at) AS last_seen_at FROM public_geo.{$table} GROUP BY jurisdiction_code";
            }

            return array_map(static fn (object $r): array => [
                'layer' => (string) $r->layer,
                'jurisdiction_code' => (string) $r->jurisdiction_code,
                'rows' => (int) $r->row_count,
                'last_seen_at' => $r->last_seen_at !== null ? (string) $r->last_seen_at : null,
            ], DB::select(implode(' UNION ALL ', $selects).' ORDER BY layer, jurisdiction_code'));
        });

        $latest = collect($layers)->pluck('last_seen_at')->filter()->max();

        return response()->json(['layers' => $layers, 'last_seen_at' => $latest]);
    }
}
