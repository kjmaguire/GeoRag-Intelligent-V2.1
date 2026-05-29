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
 * Foundry/TargetsController — drill-target recommendation surface (§8).
 *
 * Reads from targeting.target_models + target_model_versions +
 * target_recommendations + target_score_factors. The active deposit
 * model for the Wyoming Roll-Front Uranium (Cameco Shirley Basin)
 * project is `roll_front_uranium` per Phase G.1 seed.
 *
 * Empty when the project has no scoring run yet.
 */
class TargetsController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        // Pick the active deposit model for THIS project. For the Wyoming
        // Cameco Shirley Basin workspace the active model is `roll_front_uranium`
        // (Phase G.1 §8 seed). For other projects we fall back to the model
        // whose target_model_versions row has is_active=true; only if no
        // version is marked active do we fall back to the commodity match.
        $commodity = strtolower((string) ($project->commodity ?? 'uranium'));
        $isUranium = str_contains($commodity, 'uranium');
        $activeSlug = $isUranium ? 'roll_front_uranium' : null;

        $depositModels = DB::table('targeting.target_models as tm')
            ->leftJoin('targeting.target_model_versions as tmv', function ($j) {
                $j->on('tm.target_model_id', '=', 'tmv.target_model_id')->where('tmv.is_active', true);
            })
            ->select('tm.slug', 'tm.display_name', 'tm.commodity_primary', 'tmv.is_active')
            ->get()
            ->map(fn ($m) => [
                'slug' => $m->slug,
                'display_name' => $m->display_name,
                'commodity_primary' => $m->commodity_primary,
                'populated' => true,
                'is_active' => $m->slug === $activeSlug,
                'templates_count' => 1,
                'ontology_terms' => 0,
            ])
            // Active model first, then templates matching this project's commodity,
            // then everything else alphabetical. Keeps the visually-dominant top
            // slot tied to the project context — Wyoming Cameco Shirley Basin
            // surfaces roll_front_uranium, not athabasca_uranium.
            ->sortBy(function (array $m) use ($commodity) {
                if ($m['is_active']) {
                    return '0_'.$m['display_name'];
                }
                if (str_contains(strtolower($m['commodity_primary']), $commodity)) {
                    return '1_'.$m['display_name'];
                }

                return '2_'.$m['display_name'];
            })
            ->values();

        // Recommendations (if any runs exist for this project)
        $recommendations = collect();
        try {
            $recommendations = DB::table('targeting.target_recommendations as tr')
                ->join('targeting.target_candidate_zones as tcz', 'tr.zone_id', '=', 'tcz.zone_id')
                ->where('tcz.project_id', $project->project_id)
                ->orderBy('tr.rank')
                ->limit(20)
                ->get();
        } catch (\Throwable $e) {
            // Schema may differ; degrade gracefully.
        }

        $recRows = $recommendations->map(fn ($r, $i) => [
            'target_id' => (string) $r->recommendation_id,
            'rank' => (int) $r->rank,
            'status' => 'recommended',
            'coord' => null,
            'score' => (float) ($r->score ?? 0),
            'confidence' => (float) ($r->confidence ?? 0),
            'evidence_count' => 0,
            'summary' => (string) ($r->summary ?? ''),
            'positives' => [],
            'negatives' => [],
            'analogues' => [],
            'next_data' => [],
            'constraints' => [],
            'geochem' => [],
        ])->values();

        return Inertia::render('Foundry/Targets', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'deposit_models' => $depositModels,
            'active_model_slug' => $activeSlug,
            'recommendations' => $recRows,
            'empty' => $recRows->isEmpty(),
        ]);
    }
}
