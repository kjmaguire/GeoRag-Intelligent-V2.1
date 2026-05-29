<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Models\QueryAuditLog;
use Carbon\CarbonImmutable;
use Illuminate\Http\Request;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/AuditLogController — NI 43-101 provenance ledger.
 *
 * Reads real audit.query_audit_log rows for the active project. The schema
 * has no explicit `status` column — refusal is inferred from response_text
 * being null (queue dispatched but not answered) vs populated.
 */
class AuditLogController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $status = $request->query('status'); // ok | refused | all
        $days = (int) ($request->query('days') ?? 30);
        $since = CarbonImmutable::now()->subDays($days);

        $base = QueryAuditLog::where('project_id', $project->project_id)->where('created_at', '>=', $since);
        if ($status === 'ok') {
            $base = $base->whereNotNull('response_text');
        } elseif ($status === 'refused') {
            $base = $base->whereNull('response_text');
        }

        $rows = (clone $base)->orderByDesc('created_at')->limit(120)->get();

        $totalCount = (int) (clone $base)->count();
        $refusedCount = (int) QueryAuditLog::where('project_id', $project->project_id)
            ->where('created_at', '>=', $since)
            ->whereNull('response_text')
            ->count();
        // PG numeric/double aggregates come back as strings via the asyncpg
        // → laravel driver path. Cast to float before round() to avoid
        // `round(): Argument #1 ($num) must be of type int|float, string given`.
        $avgLatencyRaw = (clone $base)->avg('response_time_ms');
        $avgLatency = $avgLatencyRaw === null ? 0 : (int) round((float) $avgLatencyRaw);

        $totals = [
            'queries' => $totalCount,
            'refused' => $refusedCount,
            'avg_latency_ms' => $avgLatency,
            'total_tokens' => 0, // schema doesn't track tokens directly
        ];

        $refusalPct = $totalCount === 0
            ? 0.0
            : round(((float) $refusedCount / max(1, $totalCount)) * 100, 1);

        $auditRows = $rows->map(function ($r) {
            $isRefused = $r->response_text === null;
            $citations = $r->citations ?? [];
            $citationCount = is_array($citations) ? count($citations) : 0;

            return [
                'id' => (string) ($r->audit_id ?? $r->id ?? ''),
                'run_id' => $r->query_id ? (string) $r->query_id : null,
                'created_at' => $r->created_at?->toIso8601String(),
                'user' => $r->user_id ? (string) $r->user_id : '—',
                'query_text' => mb_substr((string) ($r->query_text ?? ''), 0, 280),
                'query_class' => null,
                'model' => $r->llm_model ?? null,
                'tokens' => null,
                'latency_ms' => $r->response_time_ms !== null ? (int) $r->response_time_ms : null,
                'status' => $isRefused ? 'refused' : 'ok',
                'confidence' => $r->confidence !== null ? (float) $r->confidence : null,
                'citation_count' => $citationCount,
                'refusal_reason' => null,
            ];
        })->values();

        return Inertia::render('Foundry/AuditLog', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'totals' => $totals,
            'refusal_pct' => $refusalPct,
            'rows' => $auditRows,
            'filters' => [
                'status' => $status,
                'days' => $days,
            ],
            'empty' => $auditRows->isEmpty(),
        ]);
    }
}
