<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/SavedMapViewsController — list view of saved map states per
 * project. Reads silver.saved_map_views (shipped doc-phase 105/107/108).
 *
 * Three scopes: user / project / workspace.
 */
class SavedMapViewsController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $user = $request->user();
        $user->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $rows = collect();
        try {
            $rows = DB::table('silver.saved_map_views')
                ->where(function ($q) use ($project, $user) {
                    $q->where('project_id', $project->project_id)
                        ->orWhere('created_by', $user->id)
                        ->orWhereNotNull('workspace_id');
                })
                ->orderByDesc('updated_at')
                ->limit(50)
                ->get();
        } catch (\Throwable $e) {
            // table may not exist in some envs
        }

        $views = $rows->map(function ($r) use ($user) {
            $scope = 'user';
            if (isset($r->workspace_id) && $r->workspace_id !== null && $r->project_id === null) {
                $scope = 'workspace';
            } elseif (isset($r->project_id) && $r->project_id !== null) {
                $scope = 'project';
            }

            return [
                'id' => (string) ($r->view_id ?? $r->id ?? ''),
                'scope' => $scope,
                'name' => (string) ($r->name ?? 'Untitled view'),
                'owner' => $r->created_by == $user->id ? 'me' : (string) ($r->created_by ?? '—'),
                'updated' => isset($r->updated_at) ? (string) $r->updated_at : '—',
                'basemap' => (string) ($r->basemap ?? 'terrain'),
                'layers_count' => is_array($r->layers ?? null) ? count($r->layers) : 0,
                'viewport' => (string) ($r->viewport_summary ?? '—'),
            ];
        })->values();

        return Inertia::render('Foundry/SavedMapViews', [
            'project_id' => $project->project_id,
            'views' => $views,
            'empty' => $views->isEmpty(),
        ]);
    }
}
