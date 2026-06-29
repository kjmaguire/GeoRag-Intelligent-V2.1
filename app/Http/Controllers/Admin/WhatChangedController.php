<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Http;
use Inertia\Inertia;
use Inertia\Response;

/**
 * §9.9 What-Changed digest viewer (Phase H4 UI).
 *
 *   GET /admin/what-changed  — recent detection runs across workspaces
 */
class WhatChangedController extends Controller
{
    public function index(Request $request): Response
    {
        $this->authorize('admin');

        $response = $this->fastapi()->get(
            $this->base().'/api/v1/admin/what-changed/runs',
            ['limit' => 100],
        );

        return Inertia::render('Admin/WhatChanged', [
            'runs' => $response->ok() ? ($response->json('runs') ?? []) : [],
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
