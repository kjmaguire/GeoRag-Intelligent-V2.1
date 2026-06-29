<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

class SettingsController extends Controller
{
    public function show(Request $request): Response
    {
        $user = $request->user();
        // 2026-06-03 audit: User has no workspace_id column, so the
        // previous `?? ''` fallback always fired → the Foundry/Settings
        // page showed "Default workspace" + zero member count for every
        // user regardless of their actual tenant. Resolve from the
        // user's first owned project.
        $wsId = (string) ($user->projects()
            ->value('silver.projects.workspace_id') ?? '');

        $workspace = null;
        try {
            $workspace = DB::table('silver.workspaces')->where('workspace_id', $wsId)->first();
        } catch (\Throwable $e) { /* */
        }

        $memberCount = 0;
        try {
            $memberCount = DB::table('public.project_user')->distinct('user_id')->count('user_id');
        } catch (\Throwable $e) { /* */
        }

        return Inertia::render('Foundry/Settings', [
            'workspace' => [
                'id' => $wsId,
                'name' => (string) ($workspace->name ?? 'Default workspace'),
                'slug' => (string) ($workspace->slug ?? 'default'),
                'data_version' => (int) ($workspace->data_version ?? 0),
            ],
            'member_count' => $memberCount,
            'can_admin' => (bool) ($user->is_admin ?? false),
        ]);
    }
}
