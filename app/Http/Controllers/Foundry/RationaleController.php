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
 * Foundry/RationaleController — "Why this target?" narrative.
 *
 * Reads recommendation + score factors + analogues from targeting.* tables.
 * The dedicated `target_rationales` table is a Wave 0 follow-up; until then,
 * synthesise the rationale from existing factor rows.
 */
class RationaleController extends Controller
{
    public function show(Request $request, string $slug, string $targetId): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $rec = null;
        $rationale = null;
        $factors = collect();
        try {
            $rec = DB::table('targeting.target_recommendations')->where('recommendation_id', $targetId)->first();
            // Prefer narrative rationale from silver.target_rationales if present.
            $rationale = DB::table('silver.target_rationales')->where('recommendation_id', $targetId)->latest('updated_at')->first();
            if (!$rationale) {
                $factors = DB::table('targeting.target_score_factors')->where('score_id', $rec->score_id ?? null)->get();
            }
        } catch (\Throwable $e) {
            // Table may not exist in all envs.
        }

        if ($rationale) {
            $positives = collect(json_decode((string) $rationale->positives, true) ?? [])->map(fn ($p) => [
                'factor' => (string) ($p['factor'] ?? ''),
                'detail' => (string) ($p['detail'] ?? ''),
                'weight' => (float) ($p['weight'] ?? 0),
            ])->values();
            $negatives = collect(json_decode((string) $rationale->negatives, true) ?? [])->map(fn ($p) => [
                'factor' => (string) ($p['factor'] ?? ''),
                'detail' => (string) ($p['detail'] ?? ''),
                'weight' => (float) ($p['weight'] ?? 0),
            ])->values();
            $analogues = collect(json_decode((string) $rationale->analogues, true) ?? [])->values();
            $trajectory = collect(json_decode((string) $rationale->confidence_trajectory, true) ?? [])->values();
            $citations = collect(json_decode((string) $rationale->citations, true) ?? [])->values();
            $alternates = collect(json_decode((string) $rationale->alternates, true) ?? [])->values();
            $summaryText = (string) ($rationale->summary ?? '');
        } else {
            $positives = $factors->filter(fn ($f) => ($f->weight ?? 0) >= 0)->map(fn ($f) => [
                'factor' => (string) ($f->factor_name ?? ''),
                'detail' => (string) ($f->detail ?? ''),
                'weight' => (float) ($f->weight ?? 0),
            ])->values();
            $negatives = $factors->filter(fn ($f) => ($f->weight ?? 0) < 0)->map(fn ($f) => [
                'factor' => (string) ($f->factor_name ?? ''),
                'detail' => (string) ($f->detail ?? ''),
                'weight' => (float) ($f->weight ?? 0),
            ])->values();
            $analogues = collect();
            $trajectory = collect();
            $citations = collect();
            $alternates = collect();
            $summaryText = (string) ($rec->summary ?? '');
        }

        return Inertia::render('Foundry/Rationale', [
            'target_id' => $targetId,
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'rank' => $rec->rank ?? null,
            'coord' => null,
            'confidence' => isset($rec->confidence) ? (float) $rec->confidence : null,
            'summary' => $summaryText !== '' ? $summaryText : null,
            'positives' => $positives,
            'negatives' => $negatives,
            'analogues' => $analogues,
            'confidence_trajectory' => $trajectory,
            'alternates' => $alternates,
            'citations' => $citations,
            'deposit_model_slug' => 'roll_front_uranium',
            'empty' => $rec === null && $rationale === null,
        ]);
    }
}
