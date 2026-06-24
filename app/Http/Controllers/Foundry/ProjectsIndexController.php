<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Illuminate\Http\Request;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/ProjectsIndexController — list/picker view of all projects.
 *
 * Separate from Portfolio (which is the exec rollup dashboard). This is
 * the card grid the user lands on after clicking "Projects" in the org bar.
 */
class ProjectsIndexController extends Controller
{
    public function show(Request $request): Response
    {
        $user = $request->user();
        $projectIds = $user->projects()->pluck('silver.projects.project_id');

        $projects = Project::whereIn('project_id', $projectIds)
            ->orderBy('updated_at', 'desc')
            ->get()
            ->map(fn ($p) => [
                'project_id' => $p->project_id,
                'project_name' => $p->project_name,
                'slug' => $p->slug,
                'region' => $p->region,
                'commodity' => $p->commodity,
                'status' => is_object($p->status) ? $p->status->value : ($p->status ?? 'active'),
                'crs_epsg' => $p->crs_epsg,
                'data_version' => $p->data_version ?? 0,
                'workspace_id' => $p->workspace_id,
                'created_at' => $p->created_at?->toIso8601String(),
                'updated_at' => $p->updated_at?->toIso8601String(),
            ])
            ->values();

        // Top-level workspace_id for useWorkspaceActivity. See
        // PortfolioController for the rationale — the
        // `$user->workspace_id ?? <hardcoded>` fallback always fired
        // because User has no workspace_id column, and after the
        // Theme F channel-auth fix that subscription is denied for
        // any user whose projects aren't in the default tenant.
        // Derive from the first owned project instead.
        $resolvedWorkspaceId = $projects->isEmpty()
            ? null
            : (string) $projects->first()['workspace_id'];

        return Inertia::render('Foundry/Projects', [
            'workspace_id' => $resolvedWorkspaceId,
            'projects' => $projects,
            'empty' => $projects->isEmpty(),
        ]);
    }
}
