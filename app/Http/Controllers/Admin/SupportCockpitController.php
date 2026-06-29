<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Master-plan §10.11 / §25 — Customer Support Cockpit (doc-phase 130).
 *
 * Read-only admin surface for the §25 support stack:
 *
 *   - ticket inventory (ops.support_tickets) with per-status + per-severity
 *     + per-category rollups
 *   - paginated recent-ticket list, filter-aware
 *   - recent support_access audit anchors (audit.audit_ledger entries
 *     emitted by emit_support_access_audit + open_trace_with_audit) —
 *     forensic trail of every cross-workspace ops access
 *   - recent replay runs (ops.support_replay_runs)
 *
 * Auth: 'admin' Gate (users.is_admin = true). The ops.* schema is global
 * (NO RLS) per the doc-phase 97 design — admin sees everything.
 *
 * Route: GET /admin/support-cockpit
 *
 * Backing live helpers (already shipped):
 *   - emit_support_access_audit  (doc-phase 116) — writer side
 *   - open_trace_with_audit       (doc-phase 118) — composer
 *   - record_decision             (doc-phase 115) — adjacent (decision audits)
 *   - get_workspace_audit_excerpt (doc-phase 121) — paginated reader
 *
 * Future graduations that populate this surface:
 *   - §10.4 evaluate_workspace workflow body
 *   - §10.10 support_replay workflow body
 *   - 5 §25.4 support agents (ticket_triage, root_cause_investigation,
 *     support_packet, customer_response_drafting, escalation_routing)
 */
class SupportCockpitController extends Controller
{
    private const VALID_STATUSES = ['open', 'investigating', 'resolved', 'closed'];

    private const VALID_SEVERITIES = ['low', 'medium', 'high', 'critical'];

    private const VALID_CATEGORIES = [
        'wrong_answer', 'failed_ingestion', 'failed_report',
        'integration_issue', 'performance', 'other',
    ];

    private const VALID_CHANNELS = ['in_app', 'email', 'webhook', 'phone'];

    private const VALID_ACCESS_KINDS = [
        'workspace_state_view', 'audit_ledger_excerpt',
        'workflow_replay_dry_run', 'workflow_replay_live',
        'langfuse_trace_read', 'report_read', 'chat_history_read',
    ];

    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $filters = $request->validate([
            'status' => ['nullable', 'in:'.implode(',', self::VALID_STATUSES)],
            'severity' => ['nullable', 'in:'.implode(',', self::VALID_SEVERITIES)],
            'category' => ['nullable', 'in:'.implode(',', self::VALID_CATEGORIES)],
        ]);

        return Inertia::render('Admin/SupportCockpit', [
            'kpis' => $this->kpis(),
            'by_status' => $this->byStatus(),
            'by_severity' => $this->bySeverity(),
            'by_category' => $this->byCategory(),
            'recent_tickets' => $this->recentTickets($filters),
            'recent_accesses' => $this->recentSupportAccesses(),
            'recent_replays' => $this->recentReplays(),
            'filters' => array_filter($filters, fn ($v) => $v !== null),
            'valid_statuses' => self::VALID_STATUSES,
            'valid_severities' => self::VALID_SEVERITIES,
            'valid_categories' => self::VALID_CATEGORIES,
            // §10.13 — surface the LangFuse base so the cockpit can deep-link
            // a workflow_run_id straight into the LangFuse trace viewer. Empty
            // when LANGFUSE_BASE_URL isn't set; the page falls back to a
            // copyable trace_id in that case.
            'langfuse_base_url' => rtrim((string) env('LANGFUSE_BASE_URL', ''), '/'),
        ]);
    }

    /**
     * Top-level KPI counters.
     *
     * @return array{
     *   total_tickets: int,
     *   open_tickets: int,
     *   critical_open: int,
     *   unassigned_open: int,
     *   resolved_30d: int,
     *   mean_resolution_hours: ?float,
     *   total_support_accesses_30d: int,
     *   latest_ticket_at: ?string,
     * }
     */
    private function kpis(): array
    {
        $t = DB::selectOne(<<<'SQL'
            SELECT
                count(*) AS total,
                count(*) FILTER (WHERE status = 'open') AS open,
                count(*) FILTER (
                    WHERE status = 'open' AND severity = 'critical'
                ) AS critical_open,
                count(*) FILTER (
                    WHERE status = 'open' AND assigned_to_user_id IS NULL
                ) AS unassigned_open,
                count(*) FILTER (
                    WHERE status IN ('resolved','closed')
                      AND resolved_at >= now() - interval '30 days'
                ) AS resolved_30d,
                avg(EXTRACT(EPOCH FROM (resolved_at - reported_at)) / 3600)
                    FILTER (WHERE resolved_at IS NOT NULL)::float
                    AS mean_resolution_hours,
                max(reported_at) AS latest_at
            FROM ops.support_tickets
        SQL);

        $accesses30d = DB::selectOne(<<<'SQL'
            SELECT count(*) AS n
            FROM audit.audit_ledger
            WHERE action_type = 'support_access'
              AND created_at >= now() - interval '30 days'
        SQL);

        return [
            'total_tickets' => (int) $t->total,
            'open_tickets' => (int) $t->open,
            'critical_open' => (int) $t->critical_open,
            'unassigned_open' => (int) $t->unassigned_open,
            'resolved_30d' => (int) $t->resolved_30d,
            'mean_resolution_hours' => $t->mean_resolution_hours !== null
                ? round((float) $t->mean_resolution_hours, 2)
                : null,
            'total_support_accesses_30d' => (int) $accesses30d->n,
            'latest_ticket_at' => $t->latest_at,
        ];
    }

    /**
     * Ticket counts per status (open / investigating / resolved / closed).
     *
     * @return array<int, array{status: string, count: int}>
     */
    private function byStatus(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT status, count(*) AS n
            FROM ops.support_tickets
            GROUP BY status
            ORDER BY
                CASE status
                    WHEN 'open'           THEN 1
                    WHEN 'investigating'  THEN 2
                    WHEN 'resolved'       THEN 3
                    WHEN 'closed'         THEN 4
                    ELSE 5
                END
        SQL);

        return array_map(static fn (object $r) => [
            'status' => $r->status,
            'count' => (int) $r->n,
        ], $rows);
    }

    /**
     * Per-severity rollup (only counting non-closed tickets).
     *
     * @return array<int, array{severity: string, count: int}>
     */
    private function bySeverity(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT severity, count(*) AS n
            FROM ops.support_tickets
            WHERE status != 'closed'
            GROUP BY severity
            ORDER BY
                CASE severity
                    WHEN 'critical' THEN 1
                    WHEN 'high'     THEN 2
                    WHEN 'medium'   THEN 3
                    WHEN 'low'      THEN 4
                    ELSE 5
                END
        SQL);

        return array_map(static fn (object $r) => [
            'severity' => $r->severity,
            'count' => (int) $r->n,
        ], $rows);
    }

    /**
     * Per-category rollup.
     *
     * @return array<int, array{category: string, count: int}>
     */
    private function byCategory(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT category, count(*) AS n
            FROM ops.support_tickets
            GROUP BY category
            ORDER BY n DESC
        SQL);

        return array_map(static fn (object $r) => [
            'category' => $r->category,
            'count' => (int) $r->n,
        ], $rows);
    }

    /**
     * Recent tickets (last 50), filter-aware.
     *
     * @param array{status?: ?string, severity?: ?string, category?: ?string} $filters
     *
     * @return array<int, array{
     *   ticket_id: string,
     *   workspace_id: ?string,
     *   reported_by_user_id: ?int,
     *   reported_at: string,
     *   channel: string,
     *   category: string,
     *   description: string,
     *   severity: string,
     *   assigned_to_user_id: ?int,
     *   status: string,
     *   resolved_at: ?string,
     *   age_hours: float,
     * }>
     */
    private function recentTickets(array $filters): array
    {
        $where = '';
        $bindings = [];
        foreach (['status', 'severity', 'category'] as $field) {
            if (! empty($filters[$field])) {
                $where .= " AND {$field} = ?";
                $bindings[] = $filters[$field];
            }
        }

        $rows = DB::select(<<<SQL
            SELECT
                ticket_id::text         AS ticket_id,
                workspace_id::text      AS workspace_id,
                reported_by_user_id,
                reported_at,
                channel,
                category,
                left(description, 200)  AS description,
                severity,
                assigned_to_user_id,
                status,
                resolved_at,
                EXTRACT(EPOCH FROM (now() - reported_at)) / 3600 AS age_hours
            FROM ops.support_tickets
            WHERE 1=1 {$where}
            ORDER BY reported_at DESC
            LIMIT 50
        SQL, $bindings);

        return array_map(static fn (object $r) => [
            'ticket_id' => (string) $r->ticket_id,
            'workspace_id' => $r->workspace_id,
            'reported_by_user_id' => $r->reported_by_user_id !== null ? (int) $r->reported_by_user_id : null,
            'reported_at' => $r->reported_at,
            'channel' => $r->channel,
            'category' => $r->category,
            'description' => (string) $r->description,
            'severity' => $r->severity,
            'assigned_to_user_id' => $r->assigned_to_user_id !== null ? (int) $r->assigned_to_user_id : null,
            'status' => $r->status,
            'resolved_at' => $r->resolved_at,
            'age_hours' => round((float) $r->age_hours, 1),
        ], $rows);
    }

    /**
     * Recent support_access audit entries (last 100). Forensic trail of
     * every cross-workspace ops access.
     *
     * @return array<int, array{
     *   id: string,
     *   created_at: string,
     *   actor_id: ?int,
     *   workspace_id: ?string,
     *   target_id: ?string,
     *   access_kind: ?string,
     *   target_summary: ?string,
     * }>
     */
    private function recentSupportAccesses(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT
                id::text             AS id,
                created_at,
                actor_id,
                workspace_id::text   AS workspace_id,
                target_id,
                payload->>'access_kind'    AS access_kind,
                payload->>'target_summary' AS target_summary
            FROM audit.audit_ledger
            WHERE action_type = 'support_access'
            ORDER BY created_at DESC
            LIMIT 100
        SQL);

        return array_map(static fn (object $r) => [
            'id' => (string) $r->id,
            'created_at' => $r->created_at,
            'actor_id' => $r->actor_id !== null ? (int) $r->actor_id : null,
            'workspace_id' => $r->workspace_id,
            'target_id' => $r->target_id,
            'access_kind' => $r->access_kind,
            'target_summary' => $r->target_summary,
        ], $rows);
    }

    /**
     * Recent replay runs (last 30).
     *
     * @return array<int, array{
     *   replay_id: string,
     *   ticket_id: string,
     *   original_workflow_run_id: string,
     *   dry_run: bool,
     *   initiated_by_user_id: int,
     *   initiated_at: string,
     *   status: string,
     * }>
     */
    private function recentReplays(): array
    {
        $rows = DB::select(<<<'SQL'
            SELECT
                replay_id::text          AS replay_id,
                ticket_id::text          AS ticket_id,
                original_workflow_run_id,
                dry_run,
                initiated_by_user_id,
                initiated_at,
                status
            FROM ops.support_replay_runs
            ORDER BY initiated_at DESC
            LIMIT 30
        SQL);

        return array_map(static fn (object $r) => [
            'replay_id' => (string) $r->replay_id,
            'ticket_id' => (string) $r->ticket_id,
            'original_workflow_run_id' => $r->original_workflow_run_id,
            'dry_run' => (bool) $r->dry_run,
            'initiated_by_user_id' => (int) $r->initiated_by_user_id,
            'initiated_at' => $r->initiated_at,
            'status' => $r->status,
        ], $rows);
    }

    // -----------------------------------------------------------------
    // Phase G.5 follow-up — proxy operator clicks into the FastAPI
    // /api/v1/admin/support/agents/* endpoints. Each method authorises
    // admin, validates input, forwards via X-Service-Key, and surfaces
    // the agent's structured JSON back to the React side.
    // -----------------------------------------------------------------

    private const AGENT_ENDPOINT_MAP = [
        'ticket-triage' => 'ticket-triage',
        'support-packet' => 'support-packet',
        'root-cause-investigation' => 'root-cause-investigation',
        'customer-response-draft' => 'customer-response-draft',
        'escalation-routing' => 'escalation-routing',
    ];

    public function runAgent(Request $request, string $agent): JsonResponse
    {
        $this->authorize('admin');

        if (! array_key_exists($agent, self::AGENT_ENDPOINT_MAP)) {
            return response()->json(
                ['error' => "unknown agent '{$agent}'"],
                422,
            );
        }

        // Each agent accepts a different payload shape; let the
        // FastAPI side validate the specifics. Here we only enforce
        // the universal contract: a valid ticket UUID.
        $payload = $request->validate([
            'ticket_id' => ['required', 'uuid'],
            'trace_ids' => ['array'],
            'trace_ids.*' => ['string'],
            'resolution_summary' => ['string', 'min:1', 'max:2000'],
            'apply' => ['boolean'],
            'include_audit_anchors' => ['integer', 'min:1', 'max:50'],
            'include_recent_runs' => ['integer', 'min:1', 'max:20'],
        ]);

        $fastapiBase = rtrim(
            config('services.fastapi.internal_url')
                ?? config('services.fastapi.internal_url'),
            '/',
        );
        $serviceKey = config('services.fastapi.service_key');
        if (! $serviceKey) {
            return response()->json(
                ['error' => 'FASTAPI_SERVICE_KEY not configured'],
                500,
            );
        }

        $endpoint = self::AGENT_ENDPOINT_MAP[$agent];

        try {
            $response = Http::withHeaders(['X-Service-Key' => $serviceKey])
                ->timeout(30)
                ->post(
                    $fastapiBase."/api/v1/admin/support/agents/{$endpoint}",
                    $payload,
                );
        } catch (\Throwable $exc) {
            return response()->json(
                [
                    'error' => 'fastapi dispatch failed',
                    'reason' => $exc->getMessage(),
                ],
                502,
            );
        }

        if (! $response->ok()) {
            return response()->json(
                [
                    'error' => 'fastapi returned non-2xx',
                    'fastapi_status' => $response->status(),
                    'fastapi_body' => $response->body(),
                ],
                502,
            );
        }

        return response()->json($response->json());
    }
}
