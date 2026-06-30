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
 * Combined audit findings page (Phase H4 UI):
 *   §11.5 — Tenant Isolation Auditor findings (live state)
 *   §11.10 — audit cold-tier archival runs
 *   §6.4 — public/private boundary language violations
 *
 *   GET  /admin/audit             — combined dashboard
 *   POST /admin/audit/cold-tier-archive  — trigger dry-run archive
 */
class AuditFindingsController extends Controller
{
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $base = $this->base();
        $tenant = $this->fastapi()->get($base.'/api/v1/admin/audit/tenant-isolation-findings');
        $archive = $this->fastapi()->get($base.'/api/v1/admin/audit/cold-tier-archive-runs', ['limit' => 25]);
        $bound = $this->fastapi()->get($base.'/api/v1/admin/audit/boundary-violations', ['limit' => 25]);

        return Inertia::render('Admin/AuditFindings', [
            'tenant_isolation' => $tenant->ok() ? $tenant->json() : null,
            'archive_runs' => $archive->ok() ? ($archive->json('runs') ?? []) : [],
            'boundary_violations' => $bound->ok() ? ($bound->json('violations') ?? []) : [],
            'fastapi_error' => $tenant->ok() ? null : $tenant->body(),
        ]);
    }

    public function triggerArchive(Request $request): JsonResponse
    {
        $this->authorize('admin');

        $payload = $request->validate([
            'cutoff_before_iso' => ['required', 'date'],
            'archive_bucket' => ['nullable', 'string', 'max:80'],
            'workspace_id_scope' => ['nullable', 'uuid'],
            'dry_run' => ['nullable', 'boolean'],
        ]);

        $response = $this->fastapi()->post(
            $this->base().'/api/v1/admin/audit/cold-tier-archive', $payload,
        );
        if (! $response->ok()) {
            return response()->json(
                ['error' => 'fastapi error', 'fastapi_body' => $response->json()],
                502,
            );
        }

        return response()->json($response->json());
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
