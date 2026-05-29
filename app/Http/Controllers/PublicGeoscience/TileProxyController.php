<?php

declare(strict_types=1);

namespace App\Http\Controllers\PublicGeoscience;

use App\Http\Controllers\Controller;
use App\Support\Http\PooledHttpClient;
use Illuminate\Http\Request;
use Illuminate\Http\Response;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Symfony\Component\HttpFoundation\Response as SymfonyResponse;
use Throwable;

/**
 * Proxy MVT tile requests from the browser to Martin.
 *
 * Two tile families are handled:
 *
 *   GET /tiles/public-geoscience/{source}/{z}/{x}/{y}.pbf
 *       Public Geoscience (PGEO) — workspace-global read-only corpus. No
 *       project_id required. ETag derived from MAX(updated_at) of
 *       public_geo.jurisdictions (cached 60 s in Redis).
 *       Cache-Control: public, max-age=3600, must-revalidate
 *
 *   GET /tiles/silver/{source}/{z}/{x}/{y}.pbf?project_id={uuid}
 *       Silver workspace-scoped layers — one project per tile URL.
 *       Requires authenticated user to own the project (workspace_id check).
 *       ETag derived from silver.projects.data_version + z/x/y/project_id
 *       (1-row index-scan, sub-millisecond).
 *       Cache-Control: public, max-age=86400, must-revalidate
 *
 * ETag strategy — Option B (Martin does NOT forward etag_hash column):
 * ─────────────────────────────────────────────────────────────────────
 * Martin 1.5.0 treats only the first bytea column of a RETURNS TABLE
 * function as the tile body — the second etag_hash text column is silently
 * discarded. Therefore the proxy computes the ETag independently via a
 * cheap PG read before proxying to Martin.
 *
 * UPDATE (2026-05-07): Martin upgraded to 1.7.0 (per martin-alerts.yml
 * V1.5-06 header). The row-shape behaviour has NOT been re-tested against
 * 1.7 — if upstream fixed it, the server-side ETag derivation here can be
 * retired in favour of letting Martin emit ETag headers natively. Re-test:
 *   1. Force a tile request and inspect response headers for ETag.
 *   2. If present, this whole proxy block can be simplified to a pure
 *      reverse-proxy + If-None-Match passthrough.
 * Tracked as a follow-up; not blocking.
 *
 * Silver ETag (index scan on silver.projects, < 1 ms):
 *   md5(data_version::text || '|' || z || '|' || x || '|' || y || '|' || project_id)
 *
 * PGEO ETag (MAX aggregate on jurisdictions, cached 60 s):
 *   md5(epoch_s::bigint::text || '|' || z || '|' || x || '|' || y)
 *
 * If the inbound If-None-Match matches the computed ETag the proxy
 * returns 304 Not Modified immediately — Martin is never called.
 *
 * Cache-Control bump (PROXY-01 / PROXY-02 audit fixes):
 *   Silver  → public, max-age=86400, must-revalidate (was 300 s)
 *   PGEO    → public, max-age=3600,  must-revalidate (was 300 s)
 *
 * Martin ETag investigation — Option B confirmed (against 1.5.0):
 * ────────────────────────────────────────────────
 * Martin 1.5.0 served RETURNS TABLE functions by reading only the first
 * BYTEA column as the tile body. The second column (etag_hash text) was
 * discarded at the driver level — no ETag HTTP header was emitted.
 * Evidence: martin/tile_server/src/pg/query.rs (maplibre/martin on GitHub)
 * deserialised only row[0] as tile bytes.
 *
 * Martin 1.7.0 (deployed 2026-04-22 per martin-alerts.yml header) MAY
 * have fixed this — re-test against the upgraded server to confirm. If
 * fixed, retire this proxy's ETag derivation. Until verified, Option B
 * remains in effect.
 *
 * Auth:
 * ─────
 * Both families require the standard Sanctum session/token (auth:sanctum
 * middleware on the route group). Silver additionally checks project access
 * membership before proxying.
 *
 * Deliberate §07c-tile deviation (architecture doc):
 * ─────────────────────────────────────────────────
 * Public Geoscience has no project scope — it is a workspace-global
 * read-only corpus — so the PGEO path omits the {project_id} segment and
 * no per-jurisdiction RBAC is enforced here.
 *
 * This is safe today because every currently active jurisdiction
 * publishes under globally-permissive licenses:
 *   - CA-SK: Government of Saskatchewan Standard Unrestricted Use Data
 *            License v2.0 (permits redisplay + derivative use)
 *   - CA-BC: Open Government Licence – British Columbia v2.0 (same)
 *
 * When onboarding a jurisdiction whose license restricts tile-level
 * redistribution (and this is a license-check part of the onboarding
 * flow — see plan §08 "License diligence per new jurisdiction"), add a
 * check here along the lines of:
 *
 *   // Reject tile requests whose jurisdiction's license forbids redistribution.
 *   // Would require a `license_allows_tile_redistribution` BOOL column on
 *   // public_geo.jurisdictions and a jurisdiction→source mapping
 *   // that this method can read synchronously (Redis-cached).
 *
 * Flag this to the architecture-doc §07c-tile section when that happens.
 */
class TileProxyController extends Controller
{
    /**
     * Whitelist of PGEO Martin source IDs the browser may request through
     * the public-geoscience tile proxy. Keeping this explicit (rather than
     * allow-any) prevents SSRF into arbitrary Martin endpoints.
     *
     * Tier-1 legacy table-backed sources (view names, no _fn suffix) are
     * preserved for backward compat until MapLibre is migrated to the
     * function-backed _fn endpoints (Chunk 8.5+).
     */
    private const PGEO_SOURCES = [
        // ── Tier 1 table-backed (legacy, backward compat) ───────────────────
        'pg_mines',
        'pg_mineral_occurrences',
        'pg_drillhole_collars',
        'pg_resource_potential',
        'pg_rock_samples',
        'pg_assessment_surveys',
        'pg_mineral_dispositions',
        'pg_bedrock_geology',

        // ── Tier 1 function-backed (_fn wrappers, §05d etag_hash contract) ──
        'pg_mines_fn',
        'pg_mineral_occurrences_fn',
        'pg_drillhole_collars_fn',
        'pg_resource_potential_fn',
        'pg_rock_samples_fn',
        'pg_assessment_surveys_fn',
        'pg_mineral_dispositions_fn',
        'pg_bedrock_geology_fn',

        // ── SMDI standalone (plan v1.1, 2026-05-24) ─────────────────────────
        // public.smdi_deposits — parallel to pg_mineral_occurrences. See
        // docs/handoffs/smdi_ingestion_2026_05_25.md for the unification
        // question.
        'smdi_deposits',
    ];

    /**
     * Whitelist of Silver function sources the browser may request through
     * the silver tile proxy. Each entry requires a project_id query param
     * and a project access check.
     */
    private const SILVER_SOURCES = [
        'pg_collars_by_project',
        'pg_drill_traces_by_project',
        'pg_boundaries_by_project',
        'pg_formations_by_project',
        'pg_historic_workings_by_project',
        'pg_seismic_by_project',
        'pg_geochem_by_project',
        // 2026-05-20 — drillhole significant-intersection layer.
        'significant_intersections_by_project',
    ];

    /**
     * Legacy constant kept for code that still references ALLOWED_SOURCES
     * directly (e.g. existing tests that inspect the constant).
     *
     * @deprecated Use PGEO_SOURCES. Will be removed in Chunk 8.7.
     */
    private const ALLOWED_SOURCES = self::PGEO_SOURCES;

    /**
     * Cache TTL (seconds) for the PGEO jurisdiction epoch used in ETag
     * derivation. Keeps the MAX aggregate query off the hot tile path.
     */
    private const PGEO_EPOCH_CACHE_TTL = 60;

    /**
     * Per-source Cache-Control max-age (seconds), tuned to how often the
     * underlying data actually changes.
     *
     * Why differentiate:
     *   - Boundaries / formations / bedrock geology are effectively static
     *     (regulatory polygons; mapped geology). Map pans should hit the
     *     browser cache for hours, not seconds.
     *   - Drill traces + collars + geochem move with active programs. Stale
     *     tiles hide the freshly imported holes a geologist just ingested,
     *     which is the #1 "why isn't my data on the map" support ticket.
     *
     * Anything not listed falls back to the family default (PGEO_DEFAULT_MAX_AGE
     * / SILVER_DEFAULT_MAX_AGE) so adding a new source can't accidentally
     * inherit a stale TTL.
     */
    private const SOURCE_MAX_AGE = [
        // ── Silver workspace layers ─────────────────────────────────────
        'pg_boundaries_by_project' => 86400,  // 24 h — claim polygons
        'pg_formations_by_project' => 86400,  // 24 h — mapped geology
        'pg_seismic_by_project' => 43200,  // 12 h — survey footprints
        'pg_historic_workings_by_project' => 43200,  // 12 h — historical points
        'pg_drill_traces_by_project' => 900,    // 15 min — active programs
        'pg_collars_by_project' => 900,    // 15 min — active programs
        'pg_geochem_by_project' => 1800,   // 30 min — sample ingest
        'significant_intersections_by_project' => 1800,  // 30 min — recomputed on every Dagster silver→gold pass
        // ── PGEO tiles ──────────────────────────────────────────────────
        'pg_bedrock_geology' => 86400,  // 24 h — published maps
        'pg_bedrock_geology_fn' => 86400,
        'pg_mineral_dispositions' => 14400,  // 4 h — tenure churn
        'pg_mineral_dispositions_fn' => 14400,
        'pg_resource_potential' => 86400,
        'pg_resource_potential_fn' => 86400,
    ];

    private const PGEO_DEFAULT_MAX_AGE = 3600;

    private const SILVER_DEFAULT_MAX_AGE = 86400;

    /**
     * Resolve the per-source max-age, falling back to the family default.
     */
    private function maxAgeForSource(string $source, int $defaultMaxAge): int
    {
        return self::SOURCE_MAX_AGE[$source] ?? $defaultMaxAge;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Public Geoscience tile endpoint
    // GET /tiles/public-geoscience/{source}/{z}/{x}/{y}.pbf
    // ─────────────────────────────────────────────────────────────────────────

    public function tile(
        Request $request,
        string $source,
        int $z,
        int $x,
        int $y,
    ): SymfonyResponse {
        if (! in_array($source, self::PGEO_SOURCES, true)) {
            return response()->json(
                ['message' => "Unknown tile source '{$source}'."],
                404,
            );
        }

        if ($z < 0 || $z > 24 || $x < 0 || $y < 0) {
            return response()->json(['message' => 'Invalid tile coordinate.'], 400);
        }

        // ── ETag derivation (Option B — Martin does not emit ETag headers) ───
        $dbStart = microtime(true);
        $etag = $this->computePgeoEtag($z, $x, $y);
        $dbMs = round((microtime(true) - $dbStart) * 1000, 2);
        $maxAge = $this->maxAgeForSource($source, self::PGEO_DEFAULT_MAX_AGE);

        // ── 304 short-circuit (save Martin round-trip on cache hit) ──────────
        $inm = $request->header('If-None-Match');
        if ($inm !== null && $inm !== '' && $this->etagMatches($inm, $etag)) {
            return response('', 304)
                ->header('ETag', "\"{$etag}\"")
                ->header('Cache-Control', "public, max-age={$maxAge}, must-revalidate")
                ->header('Server-Timing', "db;dur={$dbMs}");
        }

        $tileStart = microtime(true);
        $upstream = $this->fetchFromMartin($request, $source, $z, $x, $y);
        $tileMs = round((microtime(true) - $tileStart) * 1000, 1);

        if ($upstream instanceof SymfonyResponse) {
            return $upstream;
        }

        [$body, $status, $martinHeaders] = $upstream;

        if ($status === 204) {
            return response('', 204)
                ->header('Cache-Control', 'public, max-age=60')
                ->header('Server-Timing', "db;dur={$dbMs}, tile;dur={$tileMs}");
        }

        return $this->buildTileResponse(
            body: $body,
            martinHeaders: $martinHeaders,
            etag: $etag,
            dbMs: $dbMs,
            tileMs: $tileMs,
            source: $source,
            z: $z,
            x: $x,
            y: $y,
            cacheMaxAge: $maxAge,
        );
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Silver tile endpoint
    // GET /tiles/silver/{source}/{z}/{x}/{y}.pbf?project_id={uuid}
    // ─────────────────────────────────────────────────────────────────────────

    public function silverTile(
        Request $request,
        string $source,
        int $z,
        int $x,
        int $y,
    ): SymfonyResponse {
        if (! in_array($source, self::SILVER_SOURCES, true)) {
            return response()->json(
                ['message' => "Unknown tile source '{$source}'."],
                404,
            );
        }

        if ($z < 0 || $z > 24 || $x < 0 || $y < 0) {
            return response()->json(['message' => 'Invalid tile coordinate.'], 400);
        }

        $projectId = $request->query('project_id');
        if ($projectId === null || $projectId === '' || ! $this->isValidUuid((string) $projectId)) {
            return response()->json(
                ['message' => 'project_id query parameter is required and must be a valid UUID.'],
                400,
            );
        }

        $projectId = (string) $projectId;

        // ── Workspace / project access check ─────────────────────────────────
        if (! $this->userHasProjectAccess($projectId)) {
            return response()->json(
                ['message' => 'Access denied to this project.'],
                403,
            );
        }

        // ── ETag derivation (cheap index scan on silver.projects) ────────────
        $dbStart = microtime(true);
        $etag = $this->computeSilverEtag($projectId, $z, $x, $y);
        $dbMs = round((microtime(true) - $dbStart) * 1000, 2);
        $maxAge = $this->maxAgeForSource($source, self::SILVER_DEFAULT_MAX_AGE);

        // ── 304 short-circuit ────────────────────────────────────────────────
        $inm = $request->header('If-None-Match');
        if ($inm !== null && $inm !== '' && $etag !== null && $this->etagMatches($inm, $etag)) {
            return response('', 304)
                ->header('ETag', "\"{$etag}\"")
                ->header('Cache-Control', "public, max-age={$maxAge}, must-revalidate")
                ->header('Server-Timing', "db;dur={$dbMs}");
        }

        $tileStart = microtime(true);
        $upstream = $this->fetchFromMartin($request, $source, $z, $x, $y, ['project_id' => $projectId]);
        $tileMs = round((microtime(true) - $tileStart) * 1000, 1);

        if ($upstream instanceof SymfonyResponse) {
            return $upstream;
        }

        [$body, $status, $martinHeaders] = $upstream;

        if ($status === 204) {
            return response('', 204)
                ->header('Cache-Control', 'public, max-age=60')
                ->header('Server-Timing', "db;dur={$dbMs}, tile;dur={$tileMs}");
        }

        return $this->buildTileResponse(
            body: $body,
            martinHeaders: $martinHeaders,
            etag: $etag,
            dbMs: $dbMs,
            tileMs: $tileMs,
            source: $source,
            z: $z,
            x: $x,
            y: $y,
            cacheMaxAge: $maxAge,
        );
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Private helpers
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Proxy a tile request to Martin and return a raw result triple or a
     * passthrough SymfonyResponse on connectivity failure.
     *
     * @param array<string,string> $extraQuery Additional query params for Martin.
     *
     * @return SymfonyResponse|array{0:string,1:int,2:array<string,string>}
     */
    private function fetchFromMartin(
        Request $request,
        string $source,
        int $z,
        int $x,
        int $y,
        array $extraQuery = [],
    ): SymfonyResponse|array {
        $base = rtrim((string) config('services.martin.internal_url'), '/');
        $timeout = (int) config('services.martin.request_timeout', 15);
        $url = sprintf('%s/%s/%d/%d/%d', $base, $source, $z, $x, $y);

        // Forward Accept-Encoding so Martin returns pre-compressed tiles.
        $acceptEncoding = $request->header('Accept-Encoding', 'gzip');

        try {
            // Pooled HTTP keeps the TCP socket to Martin alive across requests
            // in the same Octane worker — pan/zoom bursts of 40-80 tiles no
            // longer pay curl-handle setup per tile.
            $client = app(PooledHttpClient::class)
                ->forBaseUrl($base, $timeout)
                ->withHeaders([
                    'Accept-Encoding' => $acceptEncoding,
                    'Accept' => 'application/x-protobuf',
                ])
                ->withOptions(['decode_content' => false]); // preserve gzip bytes

            if (! empty($extraQuery)) {
                $client = $client->withQueryParameters($extraQuery);
            }

            $response = $client->get($url);
        } catch (Throwable $e) {
            Log::warning('Martin tile fetch failed', [
                'source' => $source,
                'z' => $z,
                'x' => $x,
                'y' => $y,
                'error' => $e->getMessage(),
            ]);

            return response('', 502);
        }

        if ($response->status() === 204) {
            return ['', 204, []];
        }

        if (! $response->successful()) {
            return response('', $response->status());
        }

        $headers = [];
        if ($ct = $response->header('Content-Type')) {
            $headers['Content-Type'] = $ct;
        }
        if ($ce = $response->header('Content-Encoding')) {
            $headers['Content-Encoding'] = $ce;
        }

        return [$response->body(), 200, $headers];
    }

    /**
     * Build the final 200 tile Response with ETag, Cache-Control, and
     * observability headers (Server-Timing, X-Tile-*).
     *
     * @param array<string,string> $martinHeaders
     */
    private function buildTileResponse(
        string $body,
        array $martinHeaders,
        ?string $etag,
        float $dbMs,
        float $tileMs,
        string $source,
        int $z,
        int $x,
        int $y,
        int $cacheMaxAge,
    ): Response {
        $headers = [
            'Content-Type' => $martinHeaders['Content-Type'] ?? 'application/x-protobuf',
            'Cache-Control' => "public, max-age={$cacheMaxAge}, must-revalidate",
            'Server-Timing' => "db;dur={$dbMs}, tile;dur={$tileMs}",
            'X-Tile-Source' => $source,
            'X-Tile-Coord' => "{$z}/{$x}/{$y}",
            'X-Tile-Bytes' => (string) strlen($body),
        ];

        if (isset($martinHeaders['Content-Encoding'])) {
            $headers['Content-Encoding'] = $martinHeaders['Content-Encoding'];
        }

        if ($etag !== null) {
            $headers['ETag'] = "\"{$etag}\"";
        }

        Log::debug('Tile proxy', [
            'source' => $source,
            'z' => $z,
            'x' => $x,
            'y' => $y,
            'status' => 200,
            'bytes' => strlen($body),
            'db_ms' => $dbMs,
            'tile_ms' => $tileMs,
            'etag' => $etag,
        ]);

        return new Response($body, 200, $headers);
    }

    /**
     * Compute the ETag for a PGEO tile.
     *
     * Uses MAX(updated_at) over public_geo.jurisdictions as the
     * version signal, epoch-truncated to whole seconds. The epoch value is
     * cached in Redis for PGEO_EPOCH_CACHE_TTL seconds to avoid hitting PG
     * on every tile in a map-pan burst.
     *
     * ETag = md5(epoch_s::bigint::text || '|' || z || '|' || x || '|' || y)
     */
    private function computePgeoEtag(int $z, int $x, int $y): string
    {
        /** @var int $epochS */
        $epochS = Cache::remember(
            'pgeo_jurisdiction_epoch',
            self::PGEO_EPOCH_CACHE_TTL,
            function (): int {
                $row = DB::selectOne(
                    'SELECT EXTRACT(EPOCH FROM MAX(updated_at))::bigint AS epoch_s
                     FROM public_geo.jurisdictions',
                );

                return $row?->epoch_s ?? 0;
            },
        );

        return md5("{$epochS}|{$z}|{$x}|{$y}");
    }

    /**
     * Compute the ETag for a Silver tile.
     *
     * Executes a single-row index scan on silver.projects to fetch
     * data_version. This is a microsecond-range operation against the PK
     * index — it does NOT re-execute the heavy MVT function.
     *
     * ETag = md5(data_version || '|' || z || '|' || x || '|' || y || '|' || project_id)
     *
     * Returns null when the project row is absent; in that case the proxy
     * continues without an ETag (Martin will return an empty 204 tile).
     */
    private function computeSilverEtag(string $projectId, int $z, int $x, int $y): ?string
    {
        $row = DB::selectOne(
            'SELECT data_version FROM silver.projects WHERE project_id = :pid',
            ['pid' => $projectId],
        );

        if ($row === null) {
            return null;
        }

        return md5("{$row->data_version}|{$z}|{$x}|{$y}|{$projectId}");
    }

    /**
     * Compare an inbound If-None-Match header value to a computed ETag.
     *
     * Handles strong ("hash") and weak (W/"hash") ETags in the inbound
     * header as well as a comma-separated list per RFC 7232 §3.2.
     */
    private function etagMatches(string $ifNoneMatch, string $etag): bool
    {
        $normalize = static function (string $value): string {
            $value = trim($value);
            if (str_starts_with($value, 'W/')) {
                $value = substr($value, 2);
            }

            return trim($value, '"');
        };

        $stripped = $normalize($ifNoneMatch);

        // Comma-separated list of ETags.
        if (str_contains($stripped, ',')) {
            foreach (explode(',', $stripped) as $candidate) {
                if ($normalize($candidate) === $etag) {
                    return true;
                }
            }

            return false;
        }

        return $stripped === $etag;
    }

    /**
     * Verify that the authenticated user has access to the given project.
     *
     * Delegates to User::hasProjectAccess() which checks the project_user
     * pivot. That method gracefully degrades when the pivot table is absent
     * (fails open with a warning log) — see User::hasProjectAccess().
     */
    private function userHasProjectAccess(string $projectId): bool
    {
        $user = Auth::user();
        if ($user === null) {
            return false;
        }

        return $user->hasProjectAccess($projectId);
    }

    /**
     * Validate that a string is a well-formed UUID.
     * Prevents PG parse errors from malformed project_id inputs.
     */
    private function isValidUuid(string $value): bool
    {
        return (bool) preg_match(
            '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i',
            $value,
        );
    }
}
