<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Tier 1 misc admin pages (Phase H4):
 *   /admin/source-trust  — §21.5 per-source trust scores
 *   /admin/export-gate   — §29 export compliance gate results
 *   /admin/load-test     — §11.9 k6 launcher catalogue
 */
class AdminMiscController extends Controller
{
    public function sourceTrust(Request $request): Response
    {
        $this->authorize('admin');
        $workspaceId = $request->query('workspace_id');
        $params = ['limit' => 200];
        if ($workspaceId) $params['workspace_id'] = $workspaceId;

        $response = $this->fastapi()->get(
            $this->base().'/api/v1/admin/source-trust/scores', $params,
        );
        return Inertia::render('Admin/SourceTrust', [
            'scores' => $response->ok() ? ($response->json('scores') ?? []) : [],
            'fastapi_error' => $response->ok() ? null : $response->body(),
            'filter_workspace_id' => $workspaceId,
        ]);
    }

    public function exportGate(Request $request): Response
    {
        $this->authorize('admin');
        $response = $this->fastapi()->get(
            $this->base().'/api/v1/admin/export-gate/results',
            ['limit' => 200],
        );
        return Inertia::render('Admin/ExportGate', [
            'results' => $response->ok() ? ($response->json('results') ?? []) : [],
            'fastapi_error' => $response->ok() ? null : $response->body(),
        ]);
    }

    public function loadTest(Request $request): Response
    {
        $this->authorize('admin');
        $response = $this->fastapi()->get(
            $this->base().'/api/v1/admin/load-test/scripts',
        );
        return Inertia::render('Admin/LoadTest', [
            'scripts' => $response->ok() ? ($response->json('scripts') ?? []) : [],
            'fastapi_error' => $response->ok() ? null : $response->body(),
        ]);
    }

    private function fastapi()
    {
        $key = config('services.fastapi.service_key');
        if (! $key) abort(500, 'FASTAPI_SERVICE_KEY not configured');
        return Http::withHeaders(['X-Service-Key' => $key])->timeout(30);
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
