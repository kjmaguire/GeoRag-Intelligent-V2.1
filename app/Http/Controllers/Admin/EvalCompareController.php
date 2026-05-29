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
 * Master-plan §10-v2 — Eval Compare (doc-phase 179).
 *
 * Admin-only dashboard for visualising eval-run trends + comparing
 * any two runs head-to-head. The compare action calls the §10.6
 * promotion-gate enforcer for an allow/block verdict.
 *
 * Routes:
 *   GET    /admin/eval/compare                — main page (Inertia)
 *   GET    /admin/eval/compare/runs.json      — paginated run list (XHR)
 *   GET    /admin/eval/compare/runs/{id}.json — per-set summary (XHR)
 *   POST   /admin/eval/compare/assess         — promotion-gate proxy (XHR)
 *
 * Auth: 'admin' Gate (users.is_admin = true).
 *
 * The Inertia page loads with the last 30d of runs preloaded; the
 * compare picker fetches per-set summaries on demand via XHR (no
 * extra Inertia visits).
 */
class EvalCompareController extends Controller
{
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $runs = $this->fastapiGet('/api/v1/admin/eval/runs', ['days' => 30, 'limit' => 50]);

        return Inertia::render('Admin/EvalCompare', [
            'recent_runs' => $runs['items'] ?? [],
            'workspace_id' => 'a0000000-0000-0000-0000-000000000001',
        ]);
    }

    public function runsJson(Request $request): JsonResponse
    {
        $this->authorize('admin');

        $query = $request->validate([
            'days' => ['nullable', 'integer', 'min:0', 'max:365'],
            'question_set' => ['nullable', 'string', 'max:40'],
            'limit' => ['nullable', 'integer', 'min:1', 'max:200'],
        ]);

        return response()->json(
            $this->fastapiGet('/api/v1/admin/eval/runs', $query),
        );
    }

    public function perSetJson(Request $request, string $id): JsonResponse
    {
        $this->authorize('admin');

        return response()->json(
            $this->fastapiGet("/api/v1/admin/eval/runs/{$id}/per-set-summary"),
        );
    }

    public function assess(Request $request): JsonResponse
    {
        $this->authorize('admin');

        $payload = $request->validate([
            'workspace_id' => ['required', 'uuid'],
            'candidate_run_id' => ['required', 'uuid'],
            'baseline_run_id' => ['required', 'uuid', 'different:candidate_run_id'],
            'dry_run' => ['nullable', 'boolean'],
        ]);
        $payload['actor_user_id'] = (int) $request->user()->id;

        return response()->json(
            $this->fastapiPost('/api/v1/admin/eval/assess-promotion', $payload),
        );
    }

    private function fastapiBase(): string
    {
        return rtrim(
            config('services.fastapi.internal_url')
                ?? env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'),
            '/',
        );
    }

    private function serviceKey(): string
    {
        $key = (string) env('FASTAPI_SERVICE_KEY', '');
        if ($key === '') {
            abort(500, 'FASTAPI_SERVICE_KEY not configured');
        }
        return $key;
    }

    /**
     * @param  array<string, mixed>  $query
     * @return array<string, mixed>
     */
    private function fastapiGet(string $path, array $query = []): array
    {
        $resp = Http::withHeaders(['X-Service-Key' => $this->serviceKey()])
            ->timeout(15)
            ->get($this->fastapiBase().$path, $query);
        if (! $resp->ok()) {
            abort($resp->status(), $resp->body());
        }
        return $resp->json() ?? [];
    }

    /**
     * @param  array<string, mixed>  $body
     * @return array<string, mixed>
     */
    private function fastapiPost(string $path, array $body): array
    {
        $resp = Http::withHeaders(['X-Service-Key' => $this->serviceKey()])
            ->timeout(30)
            ->post($this->fastapiBase().$path, $body);
        if (! $resp->ok()) {
            abort($resp->status(), $resp->body());
        }
        return $resp->json() ?? [];
    }
}
