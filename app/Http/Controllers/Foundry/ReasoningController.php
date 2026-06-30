<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/ReasoningController — 4-stage workbench
 * (Evidence → Reasoning → Candidates → Evidence Graph).
 *
 * Data model:
 *   - silver.hypotheses             — workspace-scoped reasoning rows
 *     (parent_question, label A/B/C, description, confidence,
 *     confidence_method, review_status, rationale)
 *   - silver.hypothesis_evidence_links — supporting / contradicting /
 *     missing / recommended_test links between hypothesis and passage
 *     (source_chunk_id), with weight and payload
 *   - silver.document_passages      — actual chunk text (joined where
 *     the chunk_id matches; some synthetic links use chunk_pgeo_*
 *     non-UUID handles that don't join cleanly — see passage_text key)
 *
 * Reasoning is workspace-scoped today (no project_id column on
 * silver.hypotheses), so every project in a workspace surfaces the
 * same set of hypotheses. Tracked in the schema-evolution backlog —
 * project-link column would let us narrow this.
 */
class ReasoningController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        // ── 1. Hypotheses for this workspace ──────────────────────────
        $hypothesesRaw = DB::table('silver.hypotheses')
            ->where('workspace_id', $project->workspace_id)
            ->orderByDesc('confidence')
            ->orderByDesc('created_at')
            ->limit(50)
            ->get();

        $hypothesisIds = $hypothesesRaw->pluck('hypothesis_id')->all();

        // ── 2. Evidence-link rollups per hypothesis ───────────────────
        $linkRollups = DB::table('silver.hypothesis_evidence_links')
            ->select('hypothesis_id', 'role', DB::raw('COUNT(*) AS n'))
            ->whereIn('hypothesis_id', $hypothesisIds)
            ->where('workspace_id', $project->workspace_id)
            ->groupBy('hypothesis_id', 'role')
            ->get()
            ->groupBy('hypothesis_id');

        // ── 3. Full evidence-link rows (joined to passage text where
        //     the chunk_id resolves to a real passage UUID; synthetic
        //     chunk_pgeo_* handles flow through with NULL excerpt). ──
        $evidenceRaw = collect();
        try {
            $evidenceRaw = DB::table('silver.hypothesis_evidence_links AS hel')
                ->leftJoin('silver.document_passages AS dp', function ($j) {
                    $j->whereRaw('dp.passage_id::text = hel.source_chunk_id');
                })
                ->select(
                    'hel.link_id',
                    'hel.hypothesis_id',
                    'hel.role',
                    'hel.weight',
                    'hel.source_chunk_id',
                    'hel.payload',
                    'dp.text AS passage_text',
                    'dp.document_id',
                )
                ->whereIn('hel.hypothesis_id', $hypothesisIds)
                ->where('hel.workspace_id', $project->workspace_id)
                ->orderByDesc('hel.weight')
                ->limit(200)
                ->get();
        } catch (\Throwable $e) {
            Log::warning('Foundry/Reasoning evidence_links query failed', [
                'slug' => $slug,
                'error' => $e->getMessage(),
            ]);
        }

        // ── 4. Graph rollup — used by stage 4 + Evidence header ───────
        $totalLinks = $evidenceRaw->count();
        $passagesIndexed = DB::table('silver.document_passages')
            ->where('workspace_id', $project->workspace_id)
            ->count();
        $reportsCount = DB::table('silver.reports')
            ->where('workspace_id', $project->workspace_id)
            ->count();
        $collarsCount = DB::table('silver.collars')
            ->where('project_id', $project->project_id)
            ->count();

        $hypotheses = $hypothesesRaw->map(function ($h) use ($linkRollups) {
            $byRole = collect($linkRollups[$h->hypothesis_id] ?? [])
                ->keyBy('role')
                ->map(fn ($r) => (int) $r->n);

            return [
                'id' => (string) $h->hypothesis_id,
                'title' => (string) ($h->parent_question ?? ''),
                'label' => (string) ($h->label ?? ''),
                'description' => (string) ($h->description ?? ''),
                'rationale' => (string) ($h->rationale ?? ''),
                'status' => (string) ($h->review_status ?? 'ai_suggested'),
                'confidence' => isset($h->confidence) ? (float) $h->confidence : null,
                'confidence_method' => (string) ($h->confidence_method ?? ''),
                'support_count' => $byRole->get('supporting', 0),
                'contradict_count' => $byRole->get('contradicting', 0),
                'missing_count' => $byRole->get('missing', 0),
                'tests_count' => $byRole->get('recommended_test', 0),
                'updated' => (string) ($h->created_at ?? ''),
            ];
        })->values();

        $evidence = $evidenceRaw->map(fn ($e) => [
            'id' => (string) $e->link_id,
            'hypothesis_id' => (string) $e->hypothesis_id,
            'role' => (string) $e->role,
            'weight' => isset($e->weight) ? (float) $e->weight : null,
            'source_chunk_id' => (string) ($e->source_chunk_id ?? ''),
            'passage_excerpt' => $e->passage_text ? mb_substr((string) $e->passage_text, 0, 220) : null,
            'document_id' => isset($e->document_id) ? (string) $e->document_id : null,
            'payload' => is_string($e->payload) ? json_decode($e->payload, true) : $e->payload,
        ])->values();

        $stats = [
            'total_hypotheses' => $hypotheses->count(),
            'total_evidence' => $totalLinks,
            'supporting' => $evidence->where('role', 'supporting')->count(),
            'contradicting' => $evidence->where('role', 'contradicting')->count(),
            'missing' => $evidence->where('role', 'missing')->count(),
            'recommended_tests' => $evidence->where('role', 'recommended_test')->count(),
            'passages_indexed' => $passagesIndexed,
            'reports_indexed' => $reportsCount,
            'collars_in_project' => $collarsCount,
        ];

        return Inertia::render('Foundry/Reasoning', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'hypotheses' => $hypotheses,
            'evidence' => $evidence,
            'stats' => $stats,
            'empty' => $hypotheses->isEmpty() && $evidence->isEmpty(),
            'scope_note' => 'Reasoning is workspace-scoped — every project in this workspace surfaces the same hypothesis set until per-project linkage lands in the schema.',
        ]);
    }
}
