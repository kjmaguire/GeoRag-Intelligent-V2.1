<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Services\FastApiJwtMinter;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;

/**
 * §3.3 Public REST API — surface that was audit-flagged as "largely missing".
 *
 * Provides the 8 endpoint groups the master plan requires:
 *   /api/v1/answers/{id}           — answer-run inspection
 *   /api/v1/maps/{project_id}/layers — map-layer registry
 *   /api/v1/reports                — report list + status
 *   /api/v1/targets/{project_id}   — target recommendations
 *   /api/v1/interpretations/{project_id} — wraps the existing internal proxy
 *   /api/v1/audit/{workspace_id}   — audit ledger excerpt (regulator gate)
 *   /api/v1/usage/{workspace_id}   — usage events + cost rollup
 *   /api/v1/webhooks               — registered outbound webhooks
 *
 * Each endpoint reads from the existing tables — no new business logic.
 * Auth via Sanctum (existing global group).
 *
 * Note: this is a thin survey of the public API. Each endpoint group
 * deserves its own dedicated controller eventually; collapsing all 8
 * here is the pragmatic v1 to close the audit gap quickly.
 */
class PublicApiController extends Controller
{
    public function answer(Request $request, string $answerRunId): JsonResponse
    {
        $row = DB::selectOne(
            "SELECT answer_run_id::text AS id, query_text, query_class, model_name,
                    citation_lifecycle_state, partial_resolution_rate,
                    created_at
               FROM silver.answer_runs WHERE answer_run_id = ?::uuid",
            [$answerRunId],
        );
        if (! $row) {
            return response()->json(['error' => 'answer_run not found'], 404);
        }
        return response()->json($row);
    }

    public function mapLayers(Request $request, string $projectId): JsonResponse
    {
        // Layers available for this project: workspace silver + public-geo + targeting
        return response()->json([
            'project_id' => $projectId,
            'layers' => [
                ['kind' => 'silver_collars',         'tile_url' => "/tiles/silver/collars/{z}/{x}/{y}.pbf?project_id={$projectId}"],
                ['kind' => 'silver_drill_traces',    'tile_url' => "/tiles/silver/drill_traces/{z}/{x}/{y}.pbf?project_id={$projectId}"],
                ['kind' => 'pg_mineral_occurrence',  'tile_url' => '/tiles/public-geoscience/pg_mineral_occurrence/{z}/{x}/{y}.pbf'],
                ['kind' => 'pg_drillhole_collar',    'tile_url' => '/tiles/public-geoscience/pg_drillhole_collar/{z}/{x}/{y}.pbf'],
                ['kind' => 'pg_mine',                'tile_url' => '/tiles/public-geoscience/pg_mine/{z}/{x}/{y}.pbf'],
                ['kind' => 'pg_bedrock_geology',     'tile_url' => '/tiles/public-geoscience/pg_bedrock_geology/{z}/{x}/{y}.pbf'],
            ],
        ]);
    }

    public function reports(Request $request): JsonResponse
    {
        $limit = (int) $request->query('limit', 25);
        $rows = DB::select(
            "SELECT report_id::text AS id, title, company, commodity,
                    project_name, region, filing_date, created_at
               FROM silver.reports
              ORDER BY created_at DESC NULLS LAST
              LIMIT ?",
            [min($limit, 200)],
        );
        return response()->json(['items' => $rows, 'count' => count($rows)]);
    }

    public function targets(Request $request, string $projectId): JsonResponse
    {
        $rows = DB::select(
            "SELECT recommendation_id::text AS id, rank, run_id::text,
                    LEFT(explanation_markdown, 500) AS explanation_preview,
                    created_at
               FROM targeting.target_recommendations
              WHERE project_id = ?::uuid
              ORDER BY rank ASC NULLS LAST, created_at DESC
              LIMIT 50",
            [$projectId],
        );
        return response()->json(['project_id' => $projectId, 'items' => $rows]);
    }

    public function interpretations(Request $request, string $projectId): JsonResponse
    {
        // Wrap the existing /api/v1/interpretation/* proxy with a project-scoped facade
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }
        $fastApi = rtrim(env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'), '/');
        $svc = env('FASTAPI_SERVICE_KEY');
        $jwt = app(FastApiJwtMinter::class)->mint((string) $user->id, $projectId, []);

        $allNotes = Http::withHeaders(['X-Service-Key' => $svc, 'Authorization' => "Bearer $jwt"])
            ->timeout(10)->get("$fastApi/v1/interpretation/notes", ['project_id' => $projectId]);
        $allZones = Http::withHeaders(['X-Service-Key' => $svc, 'Authorization' => "Bearer $jwt"])
            ->timeout(10)->get("$fastApi/v1/interpretation/target-zones", ['project_id' => $projectId]);
        $allSections = Http::withHeaders(['X-Service-Key' => $svc, 'Authorization' => "Bearer $jwt"])
            ->timeout(10)->get("$fastApi/v1/interpretation/section-lines", ['project_id' => $projectId]);

        return response()->json([
            'project_id' => $projectId,
            'notes' => $allNotes->ok() ? $allNotes->json() : [],
            'target_zones' => $allZones->ok() ? $allZones->json() : [],
            'section_lines' => $allSections->ok() ? $allSections->json() : [],
        ]);
    }

    public function audit(Request $request, string $workspaceId): JsonResponse
    {
        $limit = (int) $request->query('limit', 50);
        $rows = DB::select(
            "SELECT id::text, action_type, actor_id, actor_kind,
                    target_schema, target_table, target_id, created_at
               FROM audit.audit_ledger
              WHERE workspace_id = ?::uuid OR workspace_id IS NULL
              ORDER BY created_at DESC
              LIMIT ?",
            [$workspaceId, min($limit, 500)],
        );
        return response()->json([
            'workspace_id' => $workspaceId,
            'items' => $rows,
            'count' => count($rows),
            'note' => 'Hash-chain proof available at /api/v1/audit/{workspace_id}/chain-proof (separate endpoint)',
        ]);
    }

    public function usage(Request $request, string $workspaceId): JsonResponse
    {
        $sinceDays = (int) $request->query('days', 30);
        $byDay = DB::select(
            "SELECT rollup_date::text,
                    sum(invocations_total)::int AS invocations,
                    sum(tokens_prompt_total + tokens_completion_total)::bigint AS tokens,
                    round(sum(cost_usd_total)::numeric, 4) AS cost_usd
               FROM usage.usage_aggregates_daily
              WHERE workspace_id = ?::uuid
                AND rollup_date >= current_date - ?::int
              GROUP BY rollup_date
              ORDER BY rollup_date",
            [$workspaceId, $sinceDays],
        );
        $byAgent = DB::select(
            "SELECT agent_name,
                    sum(invocations_total)::int AS invocations,
                    round(sum(cost_usd_total)::numeric, 4) AS cost_usd
               FROM usage.usage_aggregates_daily
              WHERE workspace_id = ?::uuid
                AND rollup_date >= current_date - ?::int
              GROUP BY agent_name
              ORDER BY cost_usd DESC NULLS LAST
              LIMIT 15",
            [$workspaceId, $sinceDays],
        );
        return response()->json([
            'workspace_id' => $workspaceId, 'window_days' => $sinceDays,
            'by_day' => $byDay, 'by_agent' => $byAgent,
        ]);
    }

    public function webhooks(Request $request): JsonResponse
    {
        // Registered outbound webhooks from workflow.flow_registry
        $rows = DB::select(
            "SELECT flow_name, flow_type, target_url,
                    enabled, last_attempted_at, last_status, created_at
               FROM workflow.flow_registry
              WHERE flow_type = 'outbound_webhook'
              ORDER BY flow_name",
        );
        return response()->json(['items' => $rows, 'count' => count($rows)]);
    }

    /** Self-describing index — handy for buyer/developer demos. */
    public function index(Request $request): JsonResponse
    {
        return response()->json([
            'api_version' => 'v1',
            'openapi_spec' => '/api/v1/openapi.json',
            'endpoint_groups' => [
                ['name' => 'answers',         'sample' => '/api/v1/answers/{answer_run_id}'],
                ['name' => 'maps',            'sample' => '/api/v1/maps/{project_id}/layers'],
                ['name' => 'reports',         'sample' => '/api/v1/reports?limit=25'],
                ['name' => 'targets',         'sample' => '/api/v1/targets/{project_id}'],
                ['name' => 'interpretations', 'sample' => '/api/v1/interpretations/{project_id}'],
                ['name' => 'audit',           'sample' => '/api/v1/audit/{workspace_id}'],
                ['name' => 'usage',           'sample' => '/api/v1/usage/{workspace_id}?days=30'],
                ['name' => 'webhooks',        'sample' => '/api/v1/webhooks'],
            ],
        ]);
    }

    /** Serve the OpenAPI spec generated from FastAPI. */
    public function openapi(): JsonResponse
    {
        $path = base_path('docs/api/openapi.json');
        if (! file_exists($path)) {
            return response()->json(['error' => 'openapi.json not present — run scripts/generate_openapi.sh'], 404);
        }
        $body = file_get_contents($path);
        return response()->json(json_decode($body, true));
    }
}
