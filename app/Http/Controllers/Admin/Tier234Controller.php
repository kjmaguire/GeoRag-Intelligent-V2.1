<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Tier 2/3/4 admin pages bundled into one controller (Phase H4):
 *   /admin/recommendations       — §9.5 NBD + §9.6 Analogue test-benches
 *   /admin/qp-credentials        — QP credential CRUD
 *   /admin/workspace-members     — workspace member viewer
 *   /admin/workspace-settings    — per-workspace prefs (tone, etc.)
 *   /admin/audit-explorer        — generic audit ledger search
 *   /admin/saved-maps            — silver.saved_map_views browser
 */
class Tier234Controller extends Controller
{
    public function recommendations(Request $request): Response
    {
        $this->authorize('admin');

        return Inertia::render('Admin/Recommendations');
    }

    public function runNbd(Request $request): JsonResponse
    {
        $this->authorize('admin');
        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'project_id' => ['required', 'uuid'],
            'evidence_gaps' => ['required', 'array', 'min:1'],
            'evidence_gaps.*' => ['string', 'min:1', 'max:500'],
            'budget_ceiling_usd' => ['nullable', 'numeric', 'min:0'],
        ]);
        $resp = $this->fastapi()->post($this->base().'/api/v1/admin/recommendations/nbd', $payload);
        if (! $resp->ok()) {
            return response()->json(['error' => $resp->body()], 502);
        }

        return response()->json($resp->json());
    }

    public function runAnalogue(Request $request): JsonResponse
    {
        $this->authorize('admin');
        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'target_model_id' => ['required', 'string', 'max:80'],
            'project_attributes' => ['required', 'array'],
            'top_k' => ['nullable', 'integer', 'min:1', 'max:50'],
        ]);
        $resp = $this->fastapi()->post($this->base().'/api/v1/admin/recommendations/analogue', $payload);
        if (! $resp->ok()) {
            return response()->json(['error' => $resp->body()], 502);
        }

        return response()->json($resp->json());
    }

    public function qpCredentials(Request $request): Response
    {
        $this->authorize('admin');
        $resp = $this->fastapi()->get($this->base().'/api/v1/admin/qp-credentials');

        return Inertia::render('Admin/QpCredentials', [
            'credentials' => $resp->ok() ? ($resp->json('credentials') ?? []) : [],
            'fastapi_error' => $resp->ok() ? null : $resp->body(),
        ]);
    }

    public function createQp(Request $request): JsonResponse
    {
        $this->authorize('admin');
        $payload = $request->validate([
            'user_id' => ['required', 'integer'],
            'name' => ['required', 'string', 'max:200'],
            'issuing_body' => ['required', 'string', 'max:80'],
            'registration_number' => ['required', 'string', 'max:80'],
            'jurisdiction' => ['required', 'string', 'max:40'],
            'expires_at' => ['nullable', 'date'],
        ]);
        $resp = $this->fastapi()->post($this->base().'/api/v1/admin/qp-credentials', $payload);
        if (! $resp->ok()) {
            return response()->json(['error' => $resp->body()], 502);
        }

        return response()->json($resp->json(), 201);
    }

    public function verifyQp(Request $request, string $qp_credential_id): JsonResponse
    {
        $this->authorize('admin');
        $resp = $this->fastapi()->post(
            $this->base().'/api/v1/admin/qp-credentials/'.urlencode($qp_credential_id).'/verify',
        );
        if (! $resp->ok()) {
            return response()->json(['error' => $resp->body()], 502);
        }

        return response()->json($resp->json());
    }

    public function workspaceMembers(Request $request): Response
    {
        $this->authorize('admin');
        $ws = $request->query('workspace_id');
        $params = $ws ? ['workspace_id' => $ws] : [];
        $resp = $this->fastapi()->get($this->base().'/api/v1/admin/workspace-members', $params);

        return Inertia::render('Admin/WorkspaceMembers', [
            'members' => $resp->ok() ? ($resp->json('members') ?? []) : [],
            'fastapi_error' => $resp->ok() ? null : $resp->body(),
            'filter_workspace_id' => $ws,
        ]);
    }

    public function workspaceSettings(Request $request, string $workspace_id): Response
    {
        $this->authorize('admin');
        if (! preg_match('/^[0-9a-f-]{36}$/i', $workspace_id)) {
            abort(404);
        }
        $resp = $this->fastapi()->get($this->base()."/api/v1/admin/workspace-settings/{$workspace_id}");

        return Inertia::render('Admin/WorkspaceSettings', [
            'workspace_id' => $workspace_id,
            'settings' => $resp->ok() ? $resp->json() : null,
            'fastapi_error' => $resp->ok() ? null : $resp->body(),
        ]);
    }

    public function saveWorkspaceSettings(Request $request, string $workspace_id): JsonResponse
    {
        $this->authorize('admin');
        if (! preg_match('/^[0-9a-f-]{36}$/i', $workspace_id)) {
            abort(404);
        }
        $payload = $request->validate([
            'default_tone' => ['required', 'in:technical,executive,regulator'],
            'default_report_type' => ['nullable', 'string', 'max:60'],
            'sla_max_response_ms' => ['nullable', 'integer', 'min:0', 'max:600000'],
            'extra_payload' => ['nullable', 'array'],
        ]);
        $resp = $this->fastapi()->put($this->base()."/api/v1/admin/workspace-settings/{$workspace_id}", $payload);
        if (! $resp->ok()) {
            return response()->json(['error' => $resp->body()], 502);
        }

        return response()->json($resp->json());
    }

    // Kestra channel admin methods removed 2026-05-17 — service was
    // sunset at Phase 3 Step 7; Kestra is the integration boundary owner
    // per master-plan §1.

    public function auditExplorer(Request $request): Response
    {
        $this->authorize('admin');
        $filters = $request->validate([
            'action_type_prefix' => ['nullable', 'string', 'max:80'],
            'workspace_id' => ['nullable', 'uuid'],
            'target_id' => ['nullable', 'string', 'max:200'],
            'actor_id' => ['nullable', 'integer'],
        ]);
        $resp = $this->fastapi()->get(
            $this->base().'/api/v1/admin/audit-explorer/search',
            array_merge($filters, ['limit' => 200]),
        );

        return Inertia::render('Admin/AuditExplorer', [
            'entries' => $resp->ok() ? ($resp->json('entries') ?? []) : [],
            'filters' => array_filter($filters, fn ($v) => $v !== null),
            'fastapi_error' => $resp->ok() ? null : $resp->body(),
        ]);
    }

    public function backupsDashboard(Request $request): Response
    {
        $this->authorize('admin');
        $store = $request->query('store');
        $status = $request->query('status');
        $page = max(1, (int) $request->query('page', 1));
        $perPage = max(10, min(200, (int) $request->query('per_page', 50)));

        $query = [
            'limit' => $perPage,
            'offset' => ($page - 1) * $perPage,
        ];
        if ($store) {
            $query['store'] = $store;
        }
        if ($status) {
            $query['status'] = $status;
        }

        $snap = $this->fastapi()->get($this->base().'/api/v1/admin/backups/snapshot-runs', $query);
        $cold = $this->fastapi()->get($this->base().'/api/v1/admin/backups/cold-tier-runs', ['limit' => 50]);

        return Inertia::render('Admin/BackupsDashboard', [
            'snapshots' => $snap->ok() ? ($snap->json('items') ?? []) : [],
            'snapshots_total' => $snap->ok() ? ($snap->json('total') ?? 0) : 0,
            'cold_tier_runs' => $cold->ok() ? ($cold->json('items') ?? []) : [],
            'page' => $page,
            'per_page' => $perPage,
            'filter_store' => $store,
            'filter_status' => $status,
            'fastapi_error' => $snap->ok() ? null : $snap->body(),
        ]);
    }

    public function phaseH4Health(Request $request): Response
    {
        $this->authorize('admin');
        $resp = $this->fastapi()->timeout(30)->get(
            $this->base().'/api/v1/admin/phase-h4-health',
        );

        return Inertia::render('Admin/PhaseH4Health', [
            'health' => $resp->ok() ? $resp->json() : null,
            'fastapi_error' => $resp->ok() ? null : $resp->body(),
        ]);
    }

    public function verifyAuditChain(Request $request): JsonResponse
    {
        $this->authorize('admin');
        $filters = $request->validate([
            'since' => ['nullable', 'string'],
            'until' => ['nullable', 'string'],
            'workspace_id' => ['nullable', 'uuid'],
            'limit' => ['nullable', 'integer', 'min:1', 'max:1000000'],
        ]);
        $resp = $this->fastapi()->timeout(120)->get(
            $this->base().'/api/v1/admin/audit-explorer/verify-chain',
            array_filter($filters, fn ($v) => $v !== null),
        );
        if (! $resp->ok()) {
            return response()->json(['error' => $resp->body()], 502);
        }

        return response()->json($resp->json());
    }

    public function savedMaps(Request $request): Response
    {
        $this->authorize('admin');
        $ws = $request->query('workspace_id');
        $params = $ws ? ['workspace_id' => $ws] : [];
        $resp = $this->fastapi()->get($this->base().'/api/v1/admin/saved-maps', $params);

        return Inertia::render('Admin/SavedMaps', [
            'views' => $resp->ok() ? ($resp->json('views') ?? []) : [],
            'fastapi_error' => $resp->ok() ? null : $resp->body(),
            'filter_workspace_id' => $ws,
        ]);
    }

    public function alertsInbox(Request $request): Response
    {
        $this->authorize('admin');
        $includeAck = (bool) $request->query('include_acknowledged', false);
        $page = max(1, (int) $request->query('page', 1));
        $perPage = max(10, min(200, (int) $request->query('per_page', 50)));
        $offset = ($page - 1) * $perPage;
        $workspaceId = $request->query('workspace_id');
        $actionTypePrefix = $request->query('action_type_prefix');

        $query = [
            'limit' => $perPage,
            'offset' => $offset,
            'include_acknowledged' => $includeAck ? 'true' : 'false',
        ];
        if ($workspaceId) {
            $query['workspace_id'] = $workspaceId;
        }
        if ($actionTypePrefix) {
            $query['action_type_prefix'] = $actionTypePrefix;
        }

        $resp = $this->fastapi()->get($this->base().'/api/v1/admin/alerts-inbox', $query);
        $body = $resp->ok() ? $resp->json() : ['items' => [], 'total' => 0];

        return Inertia::render('Admin/AlertsInbox', [
            'items' => $body['items'] ?? [],
            'total' => $body['total'] ?? 0,
            'page' => $page,
            'per_page' => $perPage,
            'fastapi_error' => $resp->ok() ? null : $resp->body(),
            'include_acknowledged' => $includeAck,
            'filter_workspace_id' => $workspaceId,
            'filter_action_type_prefix' => $actionTypePrefix,
        ]);
    }

    public function acknowledgeAlert(Request $request): JsonResponse
    {
        $this->authorize('admin');
        $payload = $request->validate([
            'audit_id' => ['required', 'uuid'],
            'actor_id' => ['required', 'integer'],
        ]);
        $resp = $this->fastapi()->post(
            $this->base().'/api/v1/admin/alerts-inbox/acknowledge', $payload,
        );
        if (! $resp->ok()) {
            return response()->json(['error' => $resp->body()], 502);
        }

        return response()->json($resp->json(), 201);
    }

    private function fastapi()
    {
        $key = config('services.fastapi.service_key');
        if (! $key) {
            abort(500, 'FASTAPI_SERVICE_KEY not configured');
        }

        return Http::withHeaders(['X-Service-Key' => $key])->timeout(60);
    }

    private function base(): string
    {
        return rtrim(
            config('services.fastapi.internal_url')
                ?? config('services.fastapi.internal_url'),
            '/',
        );
    }
}
