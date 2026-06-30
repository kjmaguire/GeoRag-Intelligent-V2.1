<?php

declare(strict_types=1);

namespace App\Http\Controllers\Dashboard;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Models\QueryAuditLog;
use Carbon\CarbonImmutable;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

/**
 * Single-shot analytics payload for the ProjectAnalytics page.
 *
 *   GET /api/v1/dashboard/projects/{slug}/analytics
 *
 * Returns every panel's data in one round trip:
 *
 *   {
 *     "collars":      [...],       // for multi-hole 3D + map + timeline
 *     "surveys":      [...],       // keyed by collar_id for 3D traces
 *     "structures":   [...],       // project-wide stereonet
 *     "geochem":      [...],       // grade/CIA/REE distributions + PCA
 *     "meters_cumulative": [...],  // (date, cumulative_m) points
 *     "query_usage":  {...},       // daily volume + top queries
 *   }
 *
 * Payload size on the demo project: ~280 KB uncompressed / ~75 KB
 * gzipped (20 collars, 30 surveys, 973 structures, 344 geochem rows).
 * Fine for one fetch; we'll split if a project ever pushes past ~5 MB.
 */
class ProjectAnalyticsController extends Controller
{
    public function show(Request $request, string $slug): JsonResponse
    {
        $project = Project::where('slug', $slug)->first();
        if (! $project) {
            return response()->json(['error' => 'project_not_found'], 404);
        }

        $user = $request->user();
        if ($user && method_exists($user, 'projects')) {
            $allowed = $user->projects()->where('silver.projects.project_id', $project->project_id)->exists();
            if (! $allowed) {
                return response()->json(['error' => 'project_not_found'], 404);
            }
        }

        $pid = $project->project_id;

        // Collars with lon/lat in WGS84 for the alteration map.
        // Driver-aware: PostGIS projects via ST_Transform(geom, 4326); sqlite
        // (feature tests) has no PostGIS, so we return null lon/lat and let
        // downstream consumers render those rows without map pins. This keeps
        // the top_queries + non-spatial analytics paths testable on the
        // in-memory sqlite test harness without double-maintaining a
        // PostGIS-in-tests stack.
        $driver = DB::connection()->getDriverName();
        if ($driver === 'pgsql') {
            $collars = DB::table('silver.collars')
                ->selectRaw(
                    'collar_id, hole_id, total_depth, azimuth, dip, elevation, '
                    .'easting, northing, hole_type, status, drill_date, '
                    .'ST_X(ST_Transform(geom, 4326)) AS longitude, '
                    .'ST_Y(ST_Transform(geom, 4326)) AS latitude',
                )
                ->where('project_id', $pid)
                ->orderBy('hole_id')
                ->get();
        } else {
            $collars = DB::table('silver.collars')
                ->select([
                    'collar_id', 'hole_id', 'total_depth', 'azimuth', 'dip', 'elevation',
                    'easting', 'northing', 'hole_type', 'status', 'drill_date',
                ])
                ->where('project_id', $pid)
                ->orderBy('hole_id')
                ->get()
                ->map(function ($c) {
                    $c->longitude = null;
                    $c->latitude = null;

                    return $c;
                });
        }

        $collarIds = $collars->pluck('collar_id');

        $surveys = DB::table('silver.surveys')
            ->whereIn('collar_id', $collarIds)
            ->orderBy('collar_id')
            ->orderBy('depth')
            ->get(['collar_id', 'depth', 'azimuth', 'dip']);

        $structures = DB::table('silver.structures')
            ->whereIn('collar_id', $collarIds)
            ->get(['collar_id', 'depth', 'structure_type', 'true_dip', 'dip_direction', 'description']);

        // Geochem needs hole_id for per-hole filtering in the grade
        // distribution panel — join back to collars.
        $geochem = DB::table('silver.geochemistry as g')
            ->join('silver.collars as c', 'c.collar_id', '=', 'g.collar_id')
            ->where('c.project_id', $pid)
            ->get([
                'g.collar_id',
                'c.hole_id',
                'g.from_depth',
                'g.to_depth',
                'g.sio2_wt_pct',
                'g.al2o3_wt_pct',
                'g.fe2o3_wt_pct',
                'g.mgo_wt_pct',
                'g.cao_wt_pct',
                'g.na2o_wt_pct',
                'g.k2o_wt_pct',
                'g.mg_number',
                'g.cia',
                'g.eu_anomaly',
                'g.ree_json',
            ]);

        // Cumulative-meters-drilled curve: walk collars in drill-date order
        // and accumulate total_depth. Skip rows with NULL drill_date so
        // the timeline doesn't hop around the unknowns.
        $metersCumulative = $collars
            ->filter(fn ($c) => $c->drill_date !== null)
            ->sortBy('drill_date')
            ->values()
            ->reduce(function ($acc, $c) {
                $running = ($acc['last'] ?? 0) + (float) $c->total_depth;
                $acc['points'][] = [
                    'date' => $c->drill_date,
                    'hole_id' => $c->hole_id,
                    'meters' => (float) $c->total_depth,
                    'cumulative' => round($running, 1),
                ];
                $acc['last'] = $running;

                return $acc;
            }, ['points' => [], 'last' => 0])['points'];

        // Query-usage meta — scoped to this project's audit rows.
        $since30 = CarbonImmutable::now()->subDays(30);
        $dailyCounts = QueryAuditLog::selectRaw('DATE(created_at) AS day, COUNT(*) AS c')
            ->where('project_id', $pid)
            ->where('created_at', '>=', $since30)
            ->groupBy('day')
            ->orderBy('day')
            ->get();

        // query_text is encrypted at rest (A4): ciphertext is non-deterministic,
        // so we group by the deterministic query_text_hash and resolve one
        // representative query_text per group via Eloquent (which decrypts).
        //
        // Postgres has no MAX() for the uuid type, so we cast to text. That's
        // safe here because audit_id is UUIDv7 (HasUuids trait, Laravel 11+),
        // which is time-ordered lexicographically — MAX over text yields the
        // most-recently-inserted sample per hash. The cast to text is local
        // to the aggregate; the resulting string coerces back to uuid on the
        // subsequent whereIn lookup against the audit_id column.
        // Driver-aware: Postgres needs the ::text cast (no MAX(uuid)); sqlite
        // stores uuids as TEXT natively so plain MAX() works. Either way the
        // aggregate's return type is a uuid-string that whereIn() can match
        // against the uuid PK column.
        $maxExpr = $driver === 'pgsql' ? 'MAX(audit_id::text)' : 'MAX(audit_id)';
        $topHashes = QueryAuditLog::selectRaw("query_text_hash AS h, COUNT(*) AS c, {$maxExpr} AS sample_id")
            ->where('project_id', $pid)
            ->where('created_at', '>=', $since30)
            ->whereNotNull('query_text_hash')
            ->groupBy('query_text_hash')
            ->orderByDesc('c')
            ->limit(10)
            ->get();

        $sampleRows = QueryAuditLog::whereIn('audit_id', $topHashes->pluck('sample_id'))
            ->get()
            ->keyBy('audit_id');

        $topQueries = $topHashes->map(function ($row) use ($sampleRows) {
            $sample = $sampleRows->get($row->sample_id);

            return [
                'q' => $sample ? mb_strtolower(trim((string) $sample->query_text)) : null,
                'c' => (int) $row->c,
            ];
        })->values();

        $totalQueries = QueryAuditLog::where('project_id', $pid)
            ->where('created_at', '>=', $since30)
            ->count();

        $avgLatency = QueryAuditLog::where('project_id', $pid)
            ->whereNotNull('response_time_ms')
            ->where('created_at', '>=', $since30)
            ->avg('response_time_ms');

        return response()->json([
            'collars' => $collars,
            'surveys' => $surveys,
            'structures' => $structures,
            'geochem' => $geochem,
            'meters_cumulative' => $metersCumulative,
            'query_usage' => [
                'total_30d' => $totalQueries,
                'avg_latency_ms' => $avgLatency !== null ? (int) round($avgLatency) : null,
                'daily' => $dailyCounts,
                'top_queries' => $topQueries,
            ],
            'generated_at' => now()->toIso8601String(),
        ]);
    }
}
