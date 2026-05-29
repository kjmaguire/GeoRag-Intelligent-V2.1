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
 * Per-project dashboard endpoints.
 *
 * Historical note: these endpoints used to be fixture-only (controlled
 * by `config('dashboard.use_fixtures')`). That flag is still honoured —
 * if it's true we keep serving the baked JSON files used for the UI
 * walkthrough demo. When it's false (or absent) each method queries the
 * real underlying tables.
 *
 * Match keys + shapes to the existing fixtures in database/fixtures/
 * dashboard/ so the React client needs no change when the flag flips.
 */
class ProjectDashboardController extends Controller
{
    public function header(Request $request, string $slug): JsonResponse
    {
        $project = $this->resolveProject($request, $slug);
        if (config('dashboard.use_fixtures', true)) {
            return $this->fixtureOrQuery('project-header', $slug);
        }

        return response()->json([
            'data' => [
                'slug'                 => $project->slug ?? $slug,
                'name'                 => $project->project_name,
                'commodity'            => $project->primary_commodity ?? '—',
                'region'               => $project->region ?? '—',
                'operator'             => $project->operator ?? '—',
                'coordinate_system'    => $project->coordinate_system ?? 'unknown',
                'aoi_area_km2'         => (float) ($project->aoi_area_km2 ?? 0),
                'status'               => $project->status?->value ?? $project->status ?? 'active',
                'last_ingestion_at'    => optional($project->updated_at)?->toIso8601String(),
            ],
            'generated_at'      => now()->toIso8601String(),
            'cache_ttl_seconds' => 300,
        ]);
    }

    public function kpis(Request $request, string $slug): JsonResponse
    {
        $project = $this->resolveProject($request, $slug);
        if (config('dashboard.use_fixtures', true)) {
            return $this->fixtureOrQuery('project-kpis', $slug);
        }

        $pid = $project->project_id;
        $since7 = CarbonImmutable::now()->subDays(7);

        // "Documents" ≈ distinct source files indexed for the project.
        // Closest honest count before M2 is the collar count (one pdf per
        // report usually maps to N-collars; we'll tighten later).
        $documents = DB::table('silver.collars')->where('project_id', $pid)->count();

        // KG entities via Neo4j would be better; for now count each row
        // we know contributes a node (collar, lithology interval, etc.)
        $kgEntities = $documents
            + DB::table('silver.lithology_logs as l')
                ->join('silver.collars as c', 'c.collar_id', '=', 'l.collar_id')
                ->where('c.project_id', $pid)->count();

        $queries7d = QueryAuditLog::where('project_id', $pid)
            ->where('created_at', '>=', $since7)->count();

        // Citation resolution rate (7 d window), on answered queries only.
        $answered = QueryAuditLog::where('project_id', $pid)
            ->whereNotNull('response_text')
            ->where('created_at', '>=', $since7);
        $answeredCount = (clone $answered)->count();
        $citedCount = (clone $answered)
            ->whereRaw("citations IS NOT NULL AND jsonb_array_length(citations) > 0")
            ->count();
        $citationRate = $answeredCount > 0 ? $citedCount / $answeredCount : 0.0;

        // Latency — average and P95 from the audit log.
        $latencyStats = QueryAuditLog::selectRaw(
            "AVG(response_time_ms) AS avg_ms, "
            . "PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY response_time_ms) AS p95_ms"
        )
            ->where('project_id', $pid)
            ->whereNotNull('response_time_ms')
            ->where('created_at', '>=', $since7)
            ->first();

        return response()->json([
            'data' => [
                'documents'                  => $documents,
                'kg_entities'                => $kgEntities,
                'queries_7d'                 => $queries7d,
                'citation_resolution_rate'   => round($citationRate, 3),
                'avg_query_latency_ms'       => (int) round($latencyStats?->avg_ms ?? 0),
                'p95_query_latency_ms'       => (int) round($latencyStats?->p95_ms ?? 0),
                'trends' => [
                    // Rolling baselines live in M2; return 0 / 0 rather than fabricate
                    // deltas that don't correspond to a real baseline.
                    'documents_today_delta'        => 0,
                    'kg_entities_7d_delta'         => 0,
                    'queries_7d_delta_pct'         => 0,
                    'citation_resolution_7d_delta_pp' => 0.0,
                ],
            ],
            'generated_at'      => now()->toIso8601String(),
            'cache_ttl_seconds' => 60,
        ]);
    }

    public function aoi(Request $request, string $slug): JsonResponse
    {
        $project = $this->resolveProject($request, $slug);
        if (config('dashboard.use_fixtures', true)) {
            return $this->fixtureOrQuery('project-aoi', $slug);
        }

        $pid = $project->project_id;

        // Collar envelope drives the AOI polygon — we don't yet persist a
        // separate per-project AOI geometry (M2 §10p), so the convex hull
        // padded by ~10 % is the most honest answer we can give.
        $bounds = DB::selectOne(
            "SELECT ST_XMin(env) AS minx, ST_YMin(env) AS miny, "
            . "ST_XMax(env) AS maxx, ST_YMax(env) AS maxy "
            . "FROM (SELECT ST_Extent(geom_4326) AS env "
            . "      FROM silver.collars WHERE project_id = ? AND geom_4326 IS NOT NULL) t",
            [$pid]
        );

        $collars = DB::select(
            "SELECT hole_id, total_depth, "
            . "ST_X(geom_4326::geometry) AS lon, ST_Y(geom_4326::geometry) AS lat "
            . "FROM silver.collars "
            . "WHERE project_id = ? AND geom_4326 IS NOT NULL",
            [$pid]
        );

        $collarFeatures = array_map(fn ($c) => [
            'type'       => 'Feature',
            'properties' => [
                'hole_id'     => $c->hole_id,
                'total_depth' => (float) ($c->total_depth ?? 0),
            ],
            'geometry'   => [
                'type'        => 'Point',
                'coordinates' => [(float) $c->lon, (float) $c->lat],
            ],
        ], $collars);

        if (! $bounds || $bounds->minx === null) {
            // No collars with coordinates — return an empty-state payload
            // the AoiMap component knows how to render.
            return response()->json([
                'data' => [
                    'display_crs' => 'EPSG:3857',
                    'source_crs'  => 'EPSG:4326',
                    'aoi'         => null,
                    'features'    => [
                        'drill_collars'     => ['type' => 'FeatureCollection', 'features' => []],
                        'mineralized_zones' => ['type' => 'FeatureCollection', 'features' => []],
                    ],
                    'bounds' => null,
                ],
                'generated_at'      => now()->toIso8601String(),
                'cache_ttl_seconds' => 300,
            ]);
        }

        // Pad the envelope ~10 % so the polygon doesn't clip the outermost
        // collars on the map.
        $padX = max(0.001, ((float) $bounds->maxx - (float) $bounds->minx) * 0.1);
        $padY = max(0.001, ((float) $bounds->maxy - (float) $bounds->miny) * 0.1);
        $minx = (float) $bounds->minx - $padX;
        $maxx = (float) $bounds->maxx + $padX;
        $miny = (float) $bounds->miny - $padY;
        $maxy = (float) $bounds->maxy + $padY;

        return response()->json([
            'data' => [
                'display_crs' => 'EPSG:3857',
                'source_crs'  => 'EPSG:4326',
                'aoi' => [
                    'type'       => 'Feature',
                    'properties' => ['name' => ($project->project_name ?? $slug) . ' AOI'],
                    'geometry'   => [
                        'type'        => 'Polygon',
                        'coordinates' => [[
                            [$minx, $miny],
                            [$maxx, $miny],
                            [$maxx, $maxy],
                            [$minx, $maxy],
                            [$minx, $miny],
                        ]],
                    ],
                ],
                'features' => [
                    'drill_collars' => [
                        'type'     => 'FeatureCollection',
                        'features' => $collarFeatures,
                    ],
                    // Mineralized-zone polygons are M2 §10p (interpretation
                    // workspace output). Return an empty FC so the AoiMap
                    // component's MapLibre source still binds cleanly.
                    'mineralized_zones' => [
                        'type'     => 'FeatureCollection',
                        'features' => [],
                    ],
                ],
                'bounds' => [$minx, $miny, $maxx, $maxy],
            ],
            'generated_at'      => now()->toIso8601String(),
            'cache_ttl_seconds' => 300,
        ]);
    }

    public function kgCounts(Request $request, string $slug): JsonResponse
    {
        $project = $this->resolveProject($request, $slug);
        if (config('dashboard.use_fixtures', true)) {
            return $this->fixtureOrQuery('project-kg-counts', $slug);
        }

        $pid = $project->project_id;
        $collarsQ = DB::table('silver.collars')->where('project_id', $pid);
        $collarIds = (clone $collarsQ)->pluck('collar_id');

        $counts = [
            'DrillHole'        => $collarsQ->count(),
            'LithologyInterval'=> DB::table('silver.lithology_logs')->whereIn('collar_id', $collarIds)->count(),
            'Sample'           => DB::table('silver.samples')->whereIn('collar_id', $collarIds)->count(),
            'Structure'        => DB::table('silver.structures')->whereIn('collar_id', $collarIds)->count(),
            'Geochem'          => DB::table('silver.geochemistry')->whereIn('collar_id', $collarIds)->count(),
        ];

        return response()->json([
            'data' => [
                'counts' => $counts,
                'total'  => array_sum($counts),
            ],
            'generated_at'      => now()->toIso8601String(),
            'cache_ttl_seconds' => 300,
        ]);
    }

    public function recentQueries(Request $request, string $slug): JsonResponse
    {
        $project = $this->resolveProject($request, $slug);
        if (config('dashboard.use_fixtures', true)) {
            return $this->fixtureOrQuery('project-recent-queries', $slug);
        }

        $rows = QueryAuditLog::where('project_id', $project->project_id)
            ->orderByDesc('created_at')
            ->limit(15)
            ->get();

        $data = $rows->map(fn ($r) => [
            'query_id'          => $r->query_id,
            'asked_at'          => $r->created_at?->toIso8601String(),
            'query_text'        => $r->query_text,
            'citation_count'    => is_array($r->citations) ? count($r->citations) : 0,
            'confidence'        => $r->confidence,
            'response_time_ms'  => $r->response_time_ms,
        ]);

        return response()->json([
            'data'              => $data,
            'generated_at'      => now()->toIso8601String(),
            'cache_ttl_seconds' => 60,
        ]);
    }

    public function feedback(Request $request, string $slug): JsonResponse
    {
        $this->resolveProject($request, $slug);
        // Real user-feedback capture is M2 §10p. Return an honest empty
        // state here instead of the fabricated fixture percentages.
        if (config('dashboard.use_fixtures', true)) {
            return $this->fixtureOrQuery('project-feedback', $slug);
        }
        return response()->json([
            'data' => [
                'total'            => 0,
                'helpful'          => 0,
                'citation_issue'   => 0,
                'wrong_irrelevant' => 0,
                'positive_rate'    => null,
                'top_issue'        => null,
            ],
            'generated_at'      => now()->toIso8601String(),
            'cache_ttl_seconds' => 900,
        ]);
    }

    public function documents(Request $request, string $slug): JsonResponse
    {
        $project = $this->resolveProject($request, $slug);
        if (config('dashboard.use_fixtures', true)) {
            return $this->fixtureOrQuery('project-documents', $slug);
        }

        $pid = $project->project_id;

        // Reports filed against this project, with chunk count + parse
        // quality from the silver-stage ingestion pipeline. Chunk count
        // comes from silver.document_passages (one passage = one chunk).
        $rows = DB::select(
            "SELECT r.report_id, r.title, r.company, r.filing_date, "
            . "       r.parse_quality_pct, r.parser_used, r.is_scanned, "
            . "       r.source_file_sha256, r.created_at, "
            . "       (SELECT COUNT(*) FROM silver.document_passages p "
            . "         WHERE p.document_id = r.report_id) AS chunk_count "
            . "  FROM silver.reports r "
            . " WHERE r.project_id = ? "
            . " ORDER BY r.created_at DESC NULLS LAST, r.filing_date DESC NULLS LAST",
            [$pid]
        );

        $data = array_map(function ($r) {
            $chunkCount = (int) ($r->chunk_count ?? 0);
            $crsResolved = ($r->parse_quality_pct ?? 0) > 0 || $chunkCount > 0;

            return [
                'id'              => $r->report_id,
                'filename'        => $r->title ?: ('report-' . substr((string) $r->report_id, 0, 8)),
                'doc_type'        => $this->inferDocType($r->parser_used, $r->is_scanned),
                'source'          => $r->company ?: 'Uploaded',
                'stage'           => $chunkCount > 0 ? 'index' : 'silver',
                'chunk_count'     => $chunkCount > 0 ? $chunkCount : null,
                'crs_detected'    => null,
                'crs_status'      => $crsResolved ? 'resolved' : 'unresolved',
                'parse_quality'   => $r->parse_quality_pct !== null ? round((float) $r->parse_quality_pct, 2) : null,
                'parser_used'     => $r->parser_used,
                'ingested_at'     => $r->created_at,
            ];
        }, $rows);

        return response()->json([
            'data'              => $data,
            'generated_at'      => now()->toIso8601String(),
            'cache_ttl_seconds' => 300,
        ]);
    }

    /**
     * Best-guess document type from the parser the §04p pipeline picked.
     * The parser-used signal is more reliable than filename heuristics for
     * post-ingest classification.
     */
    private function inferDocType(?string $parser, ?bool $isScanned): string
    {
        if ($isScanned === true) {
            return 'Historical';
        }
        return match ($parser) {
            'pdfplumber', 'pdfminer.six' => 'Tech report',
            'openpyxl', 'xlsx'           => 'Drill log',
            'tesseract-tiff', 'ocr'      => 'Historical',
            'csv'                        => 'Assay',
            'docx'                       => 'Memo',
            default                      => 'Document',
        };
    }

    /**
     * Drill-hole summary — count, meters drilled, status + hole-type
     * breakdown, deepest hole, latest drill date. Always real data.
     *
     * New in this round — the Project dashboard needed a "drill
     * holes at a glance" widget that the previous version didn't have.
     */
    public function drillSummary(Request $request, string $slug): JsonResponse
    {
        $project = $this->resolveProject($request, $slug);

        $pid = $project->project_id;
        $collars = DB::table('silver.collars')
            ->where('project_id', $pid)
            ->orderByDesc('total_depth')
            ->get(['collar_id', 'hole_id', 'total_depth', 'hole_type', 'status', 'drill_date']);

        $total = $collars->count();
        $totalMeters = (float) $collars->sum('total_depth');
        $deepest = $collars->first();
        $latest = $collars->sortByDesc('drill_date')->first();

        $byStatus = $collars->groupBy('status')->map->count();
        $byType = $collars->groupBy('hole_type')->map->count();

        return response()->json([
            'data' => [
                'total_holes'      => $total,
                'total_meters'     => round($totalMeters, 1),
                'avg_depth_m'      => $total > 0 ? round($totalMeters / $total, 1) : 0,
                'deepest_hole'     => $deepest ? [
                    'hole_id'     => $deepest->hole_id,
                    'total_depth' => (float) $deepest->total_depth,
                ] : null,
                'latest_drill_date' => $latest?->drill_date,
                'by_status' => $byStatus->map(fn ($n, $k) => ['label' => $k ?: 'unknown', 'count' => $n])->values(),
                'by_type'   => $byType->map(fn ($n, $k) => ['label' => $k ?: 'unknown', 'count' => $n])->values(),
            ],
            'generated_at'      => now()->toIso8601String(),
            'cache_ttl_seconds' => 300,
        ]);
    }

    // ─── helpers ──────────────────────────────────────────────────────

    /**
     * Resolve the project by slug and enforce user access. Returns the
     * Eloquent model so downstream methods can read its attributes.
     * Falls back to a best-effort dummy when fixtures are on.
     */
    /**
     * D6 — flat, low-cost project context for the chat header banner.
     *
     * Keyed on UUID (not slug) because the chat page holds project_id
     * from the selector callback. Single endpoint, single round-trip —
     * surgically tuned so rendering the banner doesn't require three
     * separate fetches.
     *
     * Return fields are rendered by the React ProjectContextBanner in
     * resources/js/Components/ProjectContextBanner.tsx. Keep the keys
     * stable; the component fills missing fields with em-dashes.
     */
    public function context(Request $request, string $projectId): JsonResponse
    {
        $project = Project::where('project_id', $projectId)->first();
        if ($project === null) {
            return response()->json(['error' => 'project_not_found'], 404);
        }

        $user = $request->user();
        if ($user && method_exists($user, 'projects')) {
            $allowed = $user->projects()->where('silver.projects.project_id', $project->project_id)->exists();
            if (!$allowed) {
                return response()->json(['error' => 'project_not_found'], 404);
            }
        }

        // Hole count — single COUNT query, cached implicitly by the
        // PostgreSQL statement cache for the selector-driven dwell time.
        $holeCount = DB::table('silver.collars')
            ->where('project_id', $project->project_id)
            ->count();

        return response()->json([
            'data' => [
                'slug'       => $project->slug,
                'name'       => $project->project_name ?? '—',
                'commodity'  => $project->commodity ?? null,
                'region'     => $project->region ?? null,
                'crs_datum'  => $project->crs_datum ?? 'EPSG:32613',
                'hole_count' => (int) $holeCount,
            ],
            'generated_at'      => now()->toIso8601String(),
            'cache_ttl_seconds' => 300,
        ]);
    }

    private function resolveProject(Request $request, string $slug): Project
    {
        $project = Project::where('slug', $slug)->first();

        if (config('dashboard.use_fixtures', true)) {
            // Fixture mode — if the project doesn't exist, fabricate a
            // bare Project so methods that DO use real data (drillSummary)
            // can still work against the DB without the fixture walkthrough
            // blowing up.
            return $project ?? new Project(['slug' => $slug]);
        }

        if (!$project) {
            abort(404);
        }

        $user = $request->user();
        if ($user && method_exists($user, 'projects')) {
            $allowed = $user->projects()->where('silver.projects.project_id', $project->project_id)->exists();
            if (!$allowed) {
                abort(404);
            }
        }

        return $project;
    }

    private function fixtureOrQuery(string $fixtureName, string $slug): JsonResponse
    {
        if (config('dashboard.use_fixtures', true)) {
            $path = database_path("fixtures/dashboard/{$fixtureName}.json");

            if (! file_exists($path)) {
                return response()->json([
                    'error' => 'Fixture not found',
                    'code' => 'FIXTURE_MISSING',
                ], 500);
            }

            $json = json_decode(file_get_contents($path), true);
            $json['generated_at'] = now()->toIso8601String();

            return response()->json($json);
        }

        return response()->json([
            'error' => 'Live data not yet implemented',
            'code' => 'NOT_IMPLEMENTED',
        ], 501);
    }
}
