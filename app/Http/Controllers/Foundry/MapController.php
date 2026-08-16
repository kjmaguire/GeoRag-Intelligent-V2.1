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
 * Foundry/MapController — the standalone "Map" surface inside a project.
 *
 * Lands at /projects/{slug}/map. Until now MapView.tsx (GeoJSON rendering,
 * uncertainty rings, coverage-density layer) had exactly one caller anywhere
 * in the app — InlineViz.tsx, which only mounts it inline inside a chat
 * answer when FastAPI's spatial-query classifier happens to return map
 * data. There was no page a user could navigate to directly. This
 * controller + Foundry/Map.tsx close that gap.
 *
 * This controller deliberately does NOT build GeoJSON server-side. MapView
 * already has a self-fetch path (`projectId` set, `inlineGeoJson` absent,
 * `useMartinTiles=false`) that calls the existing
 * GET /api/v1/projects/{project}/collars endpoint — the same query
 * CollarController::index already runs (ST_Transform to lon/lat, paginated,
 * membership-gated). Passing `project_id` through to Foundry/Map.tsx and
 * letting MapView fetch for itself reuses that query instead of duplicating
 * it here. The only thing this controller computes is the collar COUNT
 * (identical query to OverviewController/SourcesController's KPI tallies)
 * so the page can render an accurate empty state without waiting on the
 * client-side fetch to resolve.
 *
 * Martin/MVT tile rendering is infrastructure-dead — the Martin tile server
 * and its Laravel proxy route (/tiles/*) were both removed in the same
 * frontend-trim week this page was added. Foundry/Map.tsx always renders
 * MapView with useMartinTiles={false}; do not flip that back on.
 */
class MapController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        $collarCount = (int) DB::table('silver.collars')
            ->where('project_id', $project->project_id)
            ->count();

        return Inertia::render('Foundry/Map', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
                'crs_epsg' => $project->crs_epsg,
            ],
            'collar_count' => $collarCount,
        ]);
    }
}
