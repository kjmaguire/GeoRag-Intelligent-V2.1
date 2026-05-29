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
 * Foundry/HoleCompareController — side-by-side comparison of two real
 * drill holes from silver.collars (+ silver.lithology_logs, silver.samples,
 * silver.geochemistry). Hole IDs are real `hole_id_canonical` values.
 */
class HoleCompareController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $leftId = $request->query('left');
        $rightId = $request->query('right');

        $pickable = DB::table('silver.collars')
            ->where('project_id', $project->project_id)
            ->orderBy('hole_id')
            ->limit(200)
            ->get(['hole_id', 'hole_id_canonical'])
            ->map(fn ($c) => [
                'hole_id' => $c->hole_id,
                'hole_id_canonical' => $c->hole_id_canonical,
            ])->values();

        $hydrate = function (?string $id) use ($project): ?array {
            if (! $id) {
                return null;
            }
            $collar = DB::table('silver.collars')
                ->where('project_id', $project->project_id)
                ->where(fn ($q) => $q->where('hole_id', $id)->orWhere('hole_id_canonical', $id))
                ->first();
            if (! $collar) {
                return null;
            }

            // Column names on silver.lithology_logs are `from_depth`,
            // `to_depth`, `lithology_code` (with `lithology_description`
            // as a longer text fallback). Earlier code referenced
            // `from_depth_m` / `to_depth_m` / `lithology` which never
            // existed — caused a 500 on every Compare page render
            // (caught 2026-05-25 on cameco-shirley-basin).
            $lithology = DB::table('silver.lithology_logs')
                ->where('collar_id', $collar->collar_id)
                ->orderBy('from_depth')
                ->limit(50)
                ->get(['from_depth', 'to_depth', 'lithology_code', 'lithology_description'])
                ->map(fn ($l) => [
                    'from_depth' => (float) $l->from_depth,
                    'to_depth' => (float) $l->to_depth,
                    'kind' => (string) ($l->lithology_code ?: $l->lithology_description ?: ''),
                ])->values()->all();

            return [
                'collar_id' => $collar->collar_id,
                'project_id' => $collar->project_id,
                'hole_id' => $collar->hole_id,
                'hole_id_canonical' => $collar->hole_id_canonical,
                'total_depth' => isset($collar->total_depth) ? (float) $collar->total_depth : null,
                'latitude' => $collar->latitude ?? null,
                'longitude' => $collar->longitude ?? null,
                'plss_section' => $collar->plss_section ?? null,
                'state_plane_easting' => $collar->state_plane_easting ?? null,
                'state_plane_northing' => $collar->state_plane_northing ?? null,
                'utm_easting' => $collar->utm_easting ?? null,
                'utm_northing' => $collar->utm_northing ?? null,
                'utm_zone' => $collar->utm_zone ?? null,
                'status' => $collar->status ?? null,
                'completed_at' => $collar->completed_at ?? null,
                'grade_avg' => null,
                'grade_top' => null,
                'grade_unit' => '% U3O8',
                'rock_summary' => null,
                'azimuth' => $collar->azimuth ?? null,
                'dip' => $collar->dip ?? null,
                'lithology' => $lithology,
                'intercepts' => [],
            ];
        };

        return Inertia::render('Foundry/HoleCompare', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'pickable' => $pickable,
            'left' => $hydrate($leftId),
            'right' => $hydrate($rightId),
            'empty' => $pickable->isEmpty(),
        ]);
    }
}
