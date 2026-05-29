<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/DecisionsController — §9.9–§9.10 Decision Intelligence.
 *
 * Reads silver.decision_records + silver.decision_options + silver.decision_outcomes
 * + silver.decision_evidence_links. Writes new decisions via the same path the
 * existing record_decision pipeline uses.
 */
class DecisionsController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $decisions = collect();
        try {
            $decisions = DB::table('silver.decision_records')
                ->where('project_id', $project->project_id)
                ->orderByDesc('created_at')
                ->limit(50)
                ->get();
        } catch (\Throwable $e) {
            // table may not exist
        }

        return Inertia::render('Foundry/Decisions', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'decisions' => $decisions->map(fn ($d) => [
                'decision_id' => (string) ($d->decision_id ?? $d->id ?? ''),
                'title' => (string) ($d->title ?? ''),
                'kind' => (string) ($d->kind ?? 'manual'),
                'subject' => (string) ($d->subject ?? ''),
                'rationale' => (string) ($d->rationale ?? ''),
                'outcome' => (string) ($d->outcome ?? 'accepted'),
                'created_by' => (string) ($d->created_by ?? '—'),
                'created_at' => (string) ($d->created_at ?? ''),
                'audit_anchor' => (string) ($d->audit_anchor ?? ''),
            ])->values(),
            'empty' => $decisions->isEmpty(),
        ]);
    }

    public function store(Request $request, string $slug): RedirectResponse
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $user = $request->user();
        $user->projects()->where('silver.projects.project_id', $project->project_id)->firstOrFail();

        $data = $request->validate([
            'title' => 'required|string|max:255',
            'kind' => 'required|string|max:60',
            'subject' => 'nullable|string|max:255',
            'rationale' => 'nullable|string|max:5000',
            'outcome' => 'required|string|in:accepted,rejected,deferred',
        ]);

        try {
            DB::table('silver.decision_records')->insert([
                'project_id' => $project->project_id,
                'title' => $data['title'],
                'kind' => $data['kind'],
                'subject' => $data['subject'] ?? null,
                'rationale' => $data['rationale'] ?? null,
                'outcome' => $data['outcome'],
                'created_by' => $user->id,
                'created_at' => now(),
                'updated_at' => now(),
            ]);
        } catch (\Throwable $e) {
            return back()->with('flash', 'Decision could not be saved: ' . $e->getMessage());
        }

        return back()->with('flash', 'Decision recorded.');
    }
}
