<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Phase 1 Step 6 — Shadow comparison dashboard.
 *
 * Read-mostly admin surface for ``silver.shadow_runs``: lets an operator see
 * the dual-write classification distribution, drill into any one row to
 * inspect ``diff_details``, and twiddle the platform / per-workspace
 * ``ingest_pdf_hatchet_traffic_pct`` flag during the 14-day ramp.
 *
 * Auth: 'admin' Gate (users.is_admin = true).
 *
 * Routes (all under auth:sanctum):
 *   GET   /admin/shadow-runs                         index
 *   GET   /admin/shadow-runs/{id}                    show
 *   PATCH /admin/shadow-runs/feature-flags/traffic   updateTrafficPct
 */
class ShadowRunsController extends Controller
{
    private const VALID_CLASSIFICATIONS = ['partial', 'clean', 'minor', 'divergent', 'fatal'];

    private const VALID_KINDS = ['ingest_pdf'];

    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $filters = $request->validate([
            'workspace_id' => ['nullable', 'uuid'],
            'classification' => ['nullable', 'in:'.implode(',', self::VALID_CLASSIFICATIONS)],
            'workflow_kind' => ['nullable', 'in:'.implode(',', self::VALID_KINDS)],
            'from' => ['nullable', 'date'],
            'to' => ['nullable', 'date'],
        ]);

        $query = DB::connection('pgsql')
            ->table('silver.shadow_runs')
            ->select([
                'id',
                'workspace_id',
                'workflow_kind',
                'classification',
                'minio_key',
                'correlation_token',
                'v149_duration_ms',
                'hatchet_duration_ms',
                'started_at',
                'completed_at',
                DB::raw("(v149_result    IS NOT NULL) AS has_v149"),
                DB::raw("(hatchet_result IS NOT NULL) AS has_hatchet"),
                DB::raw('error_v149    IS NOT NULL OR error_hatchet IS NOT NULL AS has_error'),
            ])
            ->orderByDesc('started_at')
            ->limit(200);

        if (! empty($filters['workspace_id'])) {
            $query->where('workspace_id', $filters['workspace_id']);
        }
        if (! empty($filters['classification'])) {
            $query->where('classification', $filters['classification']);
        }
        if (! empty($filters['workflow_kind'])) {
            $query->where('workflow_kind', $filters['workflow_kind']);
        }
        if (! empty($filters['from'])) {
            $query->where('started_at', '>=', $filters['from']);
        }
        if (! empty($filters['to'])) {
            $query->where('started_at', '<', $filters['to']);
        }

        $rows = $query->get()->map(static fn (object $r): array => [
            'id' => $r->id,
            'workspace_id' => $r->workspace_id,
            'workflow_kind' => $r->workflow_kind,
            'classification' => $r->classification,
            'minio_key' => $r->minio_key,
            'correlation_token' => $r->correlation_token,
            'v149_duration_ms' => $r->v149_duration_ms !== null ? (int) $r->v149_duration_ms : null,
            'hatchet_duration_ms' => $r->hatchet_duration_ms !== null ? (int) $r->hatchet_duration_ms : null,
            'started_at' => $r->started_at,
            'completed_at' => $r->completed_at,
            'has_v149' => (bool) $r->has_v149,
            'has_hatchet' => (bool) $r->has_hatchet,
            'has_error' => (bool) $r->has_error,
        ])->all();

        return Inertia::render('Admin/ShadowRuns/Index', [
            'shadow_runs' => $rows,
            'filters' => [
                'workspace_id' => $filters['workspace_id'] ?? null,
                'classification' => $filters['classification'] ?? null,
                'workflow_kind' => $filters['workflow_kind'] ?? null,
                'from' => $filters['from'] ?? null,
                'to' => $filters['to'] ?? null,
            ],
            'summary' => $this->summary(),
            'streak' => $this->cleanStreak(),
            'traffic_flags' => $this->trafficFlags(),
        ]);
    }

    public function show(Request $request, string $id): Response
    {
        $this->authorize('admin');
        // Defence-in-depth — UUID literal guard before the bind. Inertia
        // route binding doesn't enforce a UUID pattern by default.
        if (preg_match('/^[0-9a-fA-F-]{36}$/', $id) !== 1) {
            abort(404);
        }

        $row = DB::connection('pgsql')
            ->table('silver.shadow_runs')
            ->where('id', $id)
            ->first();

        if ($row === null) {
            abort(404);
        }

        return Inertia::render('Admin/ShadowRuns/Show', [
            'shadow_run' => [
                'id' => $row->id,
                'workspace_id' => $row->workspace_id,
                'workflow_kind' => $row->workflow_kind,
                'classification' => $row->classification,
                'minio_key' => $row->minio_key,
                'correlation_token' => $row->correlation_token,
                'v149_duration_ms' => $row->v149_duration_ms !== null ? (int) $row->v149_duration_ms : null,
                'hatchet_duration_ms' => $row->hatchet_duration_ms !== null ? (int) $row->hatchet_duration_ms : null,
                'v149_audit_run_id' => $row->v149_audit_run_id ?? null,
                'hatchet_audit_run_id' => $row->hatchet_audit_run_id ?? null,
                'started_at' => $row->started_at,
                'completed_at' => $row->completed_at,
                'error_v149' => $row->error_v149 ?? null,
                'error_hatchet' => $row->error_hatchet ?? null,
                'v149_result' => $this->decodeJson($row->v149_result ?? null),
                'hatchet_result' => $this->decodeJson($row->hatchet_result ?? null),
                'diff_details' => $this->decodeJson($row->diff_details ?? null),
            ],
        ]);
    }

    public function updateTrafficPct(Request $request): RedirectResponse
    {
        $this->authorize('admin');

        $validated = $request->validate([
            'workspace_id' => ['nullable', 'uuid'],
            'value' => ['required', 'integer', 'min:0', 'max:100'],
        ]);

        $workspaceId = $validated['workspace_id'] ?? null;
        $value = (int) $validated['value'];
        $userId = Auth::id();

        DB::transaction(static function () use ($workspaceId, $value, $userId): void {
            DB::connection('pgsql')->statement(
                "INSERT INTO workspace.feature_flags
                    (workspace_id, flag_name, int_value, updated_by, updated_at)
                 VALUES (?::uuid, 'ingest_pdf_hatchet_traffic_pct', ?, ?, now())
                 ON CONFLICT (workspace_id, flag_name) DO UPDATE
                    SET int_value = EXCLUDED.int_value,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = now()",
                [$workspaceId, $value, $userId]
            );
        });

        return redirect()->route('admin.shadow-runs')->with(
            'flash',
            sprintf(
                'ingest_pdf_hatchet_traffic_pct = %d%% for %s',
                $value,
                $workspaceId ?? 'platform default',
            ),
        );
    }

    /**
     * @return array{counts: array<string, int>, total_24h: int, last_classified_at: ?string}
     */
    private function summary(): array
    {
        $counts = DB::connection('pgsql')
            ->table('silver.shadow_runs')
            ->select('classification', DB::raw('count(*) AS n'))
            ->where('started_at', '>=', now()->subDay())
            ->groupBy('classification')
            ->pluck('n', 'classification')
            ->map(static fn ($n) => (int) $n)
            ->all();

        foreach (self::VALID_CLASSIFICATIONS as $c) {
            $counts[$c] = $counts[$c] ?? 0;
        }

        $lastClassified = DB::connection('pgsql')
            ->table('silver.shadow_runs')
            ->where('classification', '!=', 'partial')
            ->max('completed_at');

        return [
            'counts' => $counts,
            'total_24h' => array_sum($counts),
            'last_classified_at' => $lastClassified,
        ];
    }

    /**
     * Consecutive trailing days that ended with all classifications == 'clean'.
     * Required by Step 8 cutover gate (7 consecutive days at 100% w/ zero diverge).
     */
    private function cleanStreak(): int
    {
        $rows = DB::connection('pgsql')
            ->select(
                <<<'SQL'
                SELECT date_trunc('day', started_at) AS day,
                       bool_or(classification IN ('minor','divergent','fatal','partial'))
                           AS has_non_clean
                FROM silver.shadow_runs
                WHERE started_at >= now() - interval '30 days'
                GROUP BY 1
                ORDER BY 1 DESC
                SQL,
            );

        $streak = 0;
        foreach ($rows as $r) {
            if ((bool) $r->has_non_clean) {
                break;
            }
            $streak++;
        }

        return $streak;
    }

    /**
     * @return array<int, array{workspace_id: ?string, value: int}>
     */
    private function trafficFlags(): array
    {
        $rows = DB::connection('pgsql')
            ->table('workspace.feature_flags')
            ->where('flag_name', 'ingest_pdf_hatchet_traffic_pct')
            ->orderByRaw('workspace_id IS NULL DESC, workspace_id')
            ->get(['workspace_id', 'int_value']);

        return $rows->map(static fn (object $r) => [
            'workspace_id' => $r->workspace_id,
            'value' => (int) ($r->int_value ?? 0),
        ])->all();
    }

    private function decodeJson(mixed $raw): mixed
    {
        if ($raw === null || $raw === '') {
            return null;
        }
        if (is_array($raw)) {
            return $raw;
        }
        if (! is_string($raw)) {
            return null;
        }
        $decoded = json_decode($raw, true);

        return is_array($decoded) ? $decoded : null;
    }
}
