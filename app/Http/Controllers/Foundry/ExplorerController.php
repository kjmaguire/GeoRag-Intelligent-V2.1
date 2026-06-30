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
 * Foundry/ExplorerController — 4-tab drill explorer (Map / Strip log / Analysis / 3D).
 * Reads silver.collars + silver.lithology_logs + silver.samples + silver.structures.
 */
class ExplorerController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $statusFilter = $request->query('status');
        $search = $request->query('q');
        $activeHole = $request->query('hole');

        $q = DB::table('silver.collars')->where('project_id', $project->project_id);
        if ($statusFilter && $statusFilter !== 'all') {
            $q->where('status', $statusFilter);
        }
        if ($search) {
            $q->where(function ($qq) use ($search) {
                $qq->where('hole_id', 'ilike', "%{$search}%")->orWhere('hole_id_canonical', 'ilike', "%{$search}%");
            });
        }
        $collars = $q->orderBy('hole_id')->limit(500)->get();

        $detail = null;
        if ($activeHole) {
            $c = $collars->firstWhere('hole_id', $activeHole) ?? $collars->firstWhere('hole_id_canonical', $activeHole);
            if ($c) {
                $lithology = DB::table('silver.lithology_logs')
                    ->where('collar_id', $c->collar_id)
                    ->orderBy('from_depth')
                    ->limit(100)
                    ->get(['from_depth', 'to_depth', 'lithology_code', 'lithology_description', 'color']);
                $samples = DB::table('silver.samples')
                    ->where('collar_id', $c->collar_id)
                    ->orderBy('from_depth')
                    ->limit(50)
                    ->get(['sample_id', 'from_depth', 'to_depth', 'sample_type', 'commodity_assays']);
                $detail = [
                    'collar' => (array) $c,
                    'lithology' => $lithology->map(fn ($l) => [
                        'from_depth' => (float) $l->from_depth,
                        'to_depth' => (float) $l->to_depth,
                        'code' => (string) ($l->lithology_code ?? ''),
                        'description' => (string) ($l->lithology_description ?? ''),
                        'color' => $l->color ?? null,
                    ])->values(),
                    'samples' => $samples->map(function ($s) {
                        $assays = is_string($s->commodity_assays ?? null) ? json_decode($s->commodity_assays, true) : ($s->commodity_assays ?? []);

                        return [
                            'sample_id' => (string) $s->sample_id,
                            'from_depth' => (float) $s->from_depth,
                            'to_depth' => (float) $s->to_depth,
                            'type' => (string) ($s->sample_type ?? ''),
                            'assays' => $assays ?? [],
                        ];
                    })->values(),
                ];
            }
        }

        return Inertia::render('Foundry/Explorer', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'collars' => $collars->map(fn ($c) => [
                'collar_id' => (string) $c->collar_id,
                'hole_id' => (string) $c->hole_id,
                'hole_id_canonical' => (string) ($c->hole_id_canonical ?? $c->hole_id),
                'total_depth' => $c->total_depth !== null ? (float) $c->total_depth : null,
                'status' => (string) ($c->status ?? '—'),
                'easting' => $c->easting !== null ? (float) $c->easting : null,
                'northing' => $c->northing !== null ? (float) $c->northing : null,
            ])->values(),
            'detail' => $detail,
            'filters' => [
                'status' => $statusFilter,
                'search' => $search,
                'active_hole' => $activeHole,
            ],
            'empty' => $collars->isEmpty(),
        ]);
    }
}
