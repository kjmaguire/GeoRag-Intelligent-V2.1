<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\QueryAuditLog;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/SupportCockpitController — admin replay tool (§10).
 *
 * Reads silver.workspaces (workspace list) + audit.query_audit_log (recent
 * traces). Admin-only. Access events should be anchored to support_access_log
 * (existing ops_support_schema) when the operator opens a trace.
 */
class SupportCockpitController extends Controller
{
    public function show(Request $request): Response
    {
        abort_unless((bool) ($request->user()->is_admin ?? false), 403);

        $workspaces = collect();
        try {
            $workspaces = DB::table('silver.workspaces')
                ->select('workspace_id', 'name')
                ->orderBy('name')
                ->get();
        } catch (\Throwable $e) {
            // ignore
        }

        $traces = QueryAuditLog::orderByDesc('created_at')->limit(40)->get();

        $thresholds = [
            ['id' => 'min_relevance', 'label' => 'Min relevance for retrieval', 'value' => 0.72, 'min_value' => 0.5, 'max_value' => 0.95, 'unit' => ''],
            ['id' => 'min_citations', 'label' => 'Min citations for surfacing', 'value' => 2, 'min_value' => 0, 'max_value' => 6, 'unit' => ''],
            ['id' => 'refusal_floor', 'label' => 'Refusal confidence floor', 'value' => 0.55, 'min_value' => 0.3, 'max_value' => 0.8, 'unit' => ''],
            ['id' => 'rerank_topk', 'label' => 'Rerank top-k', 'value' => 8, 'min_value' => 4, 'max_value' => 20, 'unit' => ''],
            ['id' => 'agentic_max_revise', 'label' => 'Agentic revise budget', 'value' => 1, 'min_value' => 0, 'max_value' => 3, 'unit' => 'x'],
            ['id' => 'thin_evidence_floor', 'label' => 'Thin-evidence floor (passages)', 'value' => 12, 'min_value' => 4, 'max_value' => 30, 'unit' => ''],
        ];

        return Inertia::render('Foundry/SupportCockpit', [
            'workspaces' => $workspaces->map(fn ($w) => [
                'id' => (string) $w->workspace_id,
                'name' => (string) $w->name,
                'region' => 'ca-central-1',
                'users' => 0,
                'plan' => 'Pro',
                'eval_overall' => null,
                'status' => 'ok',
            ])->values(),
            'traces' => $traces->map(fn ($t) => [
                'run_id' => (string) ($t->query_id ?? $t->id),
                'workspace_id' => (string) ($t->workspace_id ?? ''),
                'user' => (string) ($t->user_id ?? '—'),
                'when' => $t->created_at?->diffForHumans() ?? '—',
                'question' => substr((string) ($t->query_text ?? ''), 0, 100),
                'status' => $t->response_text ? 'ok' : 'refused',
                'latency_ms' => (int) ($t->response_time_ms ?? 0),
                'citations' => is_array($t->citations) ? count($t->citations) : 0,
                'confidence' => isset($t->confidence) ? (float) $t->confidence : 0.0,
            ])->values(),
            'thresholds' => $thresholds,
            'can_admin' => true,
            'empty' => $workspaces->isEmpty() && $traces->isEmpty(),
        ]);
    }
}
