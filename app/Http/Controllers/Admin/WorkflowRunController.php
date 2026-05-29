<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Phase 0 Step 3 — Workflow Run Dashboard skeleton.
 *
 * Read-only admin-tier list of the most recent rows from
 * workflow.workflow_runs (a partman-monthly-partitioned Layer C table
 * deployed in Phase 0 Step 2). Operators land here from runbooks /
 * incident chatops to spot-check that orchestrators are actually
 * writing rows; richer drilldown + replay controls land in Phase 10
 * (Customer Support Cockpit).
 *
 * Auth: 'admin' Gate (users.is_admin = true), defined in
 * AppServiceProvider::boot().
 *
 * Route: GET /admin/workflow-runs (web.php, inside the auth:sanctum group).
 *
 * Filters (server-side, applied via query params):
 *   ?workspace_id=<uuid>       — narrow to a single workspace
 *   ?status=<status>           — queued|running|success|failure|cancelled|timed_out
 *   ?workflow_kind=<text>      — exact match on the kind label
 *   ?from=<ISO-8601>           — started_at >= cutoff
 *   ?to=<ISO-8601>             — started_at <  cutoff
 *
 * Last 100 rows by started_at DESC. The (workflow_kind, status, started_at DESC)
 * and (workspace_id, started_at DESC) indexes from the migration cover the
 * filter combinations we expose here.
 */
class WorkflowRunController extends Controller
{
    /**
     * Render the dashboard. Inertia page: 'Admin/WorkflowRuns'.
     */
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $filters = $this->validatedFilters($request);

        $query = DB::connection('pgsql')
            ->table('workflow.workflow_runs')
            ->select([
                'run_id',
                'workspace_id',
                'workflow_kind',
                'engine',
                'status',
                'trace_id',
                'started_at',
                'ended_at',
                'duration_ms',
                'failure_reason',
            ])
            ->orderByDesc('started_at')
            ->limit(100);

        if ($filters['workspace_id'] !== null) {
            $query->where('workspace_id', $filters['workspace_id']);
        }
        if ($filters['status'] !== null) {
            $query->where('status', $filters['status']);
        }
        if ($filters['workflow_kind'] !== null) {
            $query->where('workflow_kind', $filters['workflow_kind']);
        }
        if ($filters['from'] !== null) {
            $query->where('started_at', '>=', $filters['from']);
        }
        if ($filters['to'] !== null) {
            $query->where('started_at', '<', $filters['to']);
        }

        $rows = $query->get()->map(function (object $row): array {
            return [
                'run_id' => $row->run_id,
                'workspace_id' => $row->workspace_id,
                'workflow_kind' => $row->workflow_kind,
                'engine' => $row->engine,
                'status' => $row->status,
                'trace_id' => $row->trace_id,
                'started_at' => $row->started_at,
                'ended_at' => $row->ended_at,
                'duration_ms' => $row->duration_ms !== null ? (int) $row->duration_ms : null,
                'failure_reason' => $this->summarizeFailureReason($row->failure_reason),
            ];
        })->all();

        return Inertia::render('Admin/WorkflowRuns', [
            'workflow_runs' => $rows,
            'filters' => $filters,
            'tempo_url' => config('services.tempo.url'),
        ]);
    }

    /**
     * @return array{
     *   workspace_id: ?string,
     *   status: ?string,
     *   workflow_kind: ?string,
     *   from: ?string,
     *   to: ?string,
     * }
     */
    private function validatedFilters(Request $request): array
    {
        $validated = $request->validate([
            'workspace_id' => ['nullable', 'uuid'],
            'status' => ['nullable', 'in:queued,running,success,failure,cancelled,timed_out'],
            'workflow_kind' => ['nullable', 'string', 'max:128'],
            'from' => ['nullable', 'date'],
            'to' => ['nullable', 'date'],
        ]);

        return [
            'workspace_id' => $validated['workspace_id'] ?? null,
            'status' => $validated['status'] ?? null,
            'workflow_kind' => $validated['workflow_kind'] ?? null,
            'from' => $validated['from'] ?? null,
            'to' => $validated['to'] ?? null,
        ];
    }

    /**
     * Compress the JSONB failure_reason into a short string for the table cell.
     * Phase 10 will replace this with a click-to-expand drilldown.
     */
    private function summarizeFailureReason(mixed $raw): ?string
    {
        if ($raw === null || $raw === '') {
            return null;
        }

        $decoded = is_string($raw) ? json_decode($raw, true) : $raw;
        if (! is_array($decoded)) {
            return is_string($raw) ? mb_substr($raw, 0, 160) : null;
        }

        $message = $decoded['message'] ?? $decoded['error'] ?? $decoded['reason'] ?? null;
        if (is_string($message) && $message !== '') {
            return mb_substr($message, 0, 160);
        }

        return mb_substr((string) json_encode($decoded), 0, 160);
    }
}
