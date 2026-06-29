<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/DrillReviewController — CC-01 Item 1 Slice 4.
 *
 * Surfaces silver.review_queue rows that came out of the drill-data
 * ingest pipeline (collars / lithology / assays_v2 / samples) so a
 * geologist can approve, correct, reject, or defer each parsed record
 * before it commits to the silver-tier tables.
 *
 * Rows are grouped by ``bronze_uri`` because that uniquely identifies
 * one upload — the queue currently has no explicit ingest_batch_id and
 * the bronze_uri is the closest thing.
 *
 * Read path uses the existing SetsWorkspaceRlsContext trait so the
 * RLS policies on silver.review_queue scope the query to the workspace
 * the project belongs to.
 *
 * Decision path writes directly via DB facade — Phase 3 of the SRQ plan
 * will add a /v1/review API surface in FastAPI; this controller will be
 * refactored to proxy through it then. For v1 we keep the moving parts
 * inside Laravel so the end-to-end UX lands without a second service.
 */
class DrillReviewController extends Controller
{
    use SetsWorkspaceRlsContext;

    private const DRILL_TARGET_TABLES = [
        'silver.collars',
        'silver.lithology',
        'silver.assays_v2',
        'silver.samples',
    ];

    private const VALID_DECISIONS = [
        'approve_as_parsed',
        'approve_with_corrections',
        'reject',
        'defer',
    ];

    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        $workspaceId = (string) DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->value('workspace_id');

        return $this->withWorkspaceRls($workspaceId, function () use ($project) {
            $rows = DB::table('silver.review_queue')
                ->where('project_id', $project->project_id)
                ->whereIn('target_table', self::DRILL_TARGET_TABLES)
                ->whereIn('lifecycle', ['pending', 'in_review', 'decided'])
                ->orderBy('bronze_uri')
                ->orderBy('created_at')
                ->limit(500)
                ->get();

            $batches = $this->groupByBatch($rows);

            $counters = [
                'pending' => 0,
                'in_review' => 0,
                'decided' => 0,
            ];
            foreach ($rows as $r) {
                if (isset($counters[$r->lifecycle])) {
                    $counters[$r->lifecycle]++;
                }
            }

            return Inertia::render('Foundry/DrillReview', [
                'project' => [
                    'project_id' => $project->project_id,
                    'project_name' => $project->project_name,
                    'slug' => $project->slug,
                ],
                'batches' => $batches,
                'counters' => $counters,
                'decisions' => self::VALID_DECISIONS,
                'csrf_token' => csrf_token(),
            ]);
        });
    }

    public function decide(Request $request, string $slug, string $queueId): RedirectResponse
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        $validated = $request->validate([
            'decision_kind' => 'required|string|in:'.implode(',', self::VALID_DECISIONS),
            'decision_payload' => 'nullable|array',
            'decision_rationale' => 'nullable|string|max:2048',
        ]);

        // approve_with_corrections is the only decision that carries a
        // payload — others MUST be NULL per the SRQ schema's CHECK
        // constraint (see review_queue.py docstring).
        if ($validated['decision_kind'] !== 'approve_with_corrections') {
            $validated['decision_payload'] = null;
        }

        $workspaceId = (string) DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->value('workspace_id');

        return $this->withWorkspaceRls($workspaceId, function () use ($request, $project, $queueId, $validated) {
            $row = DB::table('silver.review_queue')
                ->where('queue_id', $queueId)
                ->where('project_id', $project->project_id)
                ->first();

            if ($row === null) {
                abort(404, 'queue row not found');
            }

            if (! in_array($row->lifecycle, ['pending', 'in_review'], true)) {
                return back()->withErrors([
                    'lifecycle' => "Row is already '{$row->lifecycle}' — cannot decide again.",
                ]);
            }

            DB::table('silver.review_queue')
                ->where('queue_id', $queueId)
                ->update([
                    'lifecycle' => 'decided',
                    'decided_by_user_id' => $request->user()->id,
                    'decision_kind' => $validated['decision_kind'],
                    'decision_payload' => $validated['decision_payload'] === null
                        ? null
                        : json_encode($validated['decision_payload']),
                    'decision_rationale' => $validated['decision_rationale'] ?? null,
                    'decided_at' => now(),
                    'updated_at' => now(),
                ]);

            // Phase 5 of the SRQ plan adds a separate "commit" job that promotes
            // decided rows into silver.*. For v1 we leave that to a follow-up
            // — the decision is captured + auditable, the silver write happens
            // when the commit job runs.

            return back()->with('status', "Decision '{$validated['decision_kind']}' recorded.");
        });
    }

    /**
     * @param iterable<int, object> $rows
     *
     * @return array<int, array<string, mixed>>
     */
    private function groupByBatch(iterable $rows): array
    {
        $grouped = [];
        foreach ($rows as $row) {
            $batchKey = $row->bronze_uri ?: 'unknown';
            if (! isset($grouped[$batchKey])) {
                $grouped[$batchKey] = [
                    'bronze_uri' => $batchKey,
                    'target_tables' => [],
                    'rows' => [],
                    'oldest' => $row->created_at,
                ];
            }
            $grouped[$batchKey]['rows'][] = $this->normaliseRow($row);
            $grouped[$batchKey]['target_tables'][$row->target_table] = true;
        }

        return array_values(array_map(static function (array $group): array {
            $group['target_tables'] = array_keys($group['target_tables']);
            $group['row_count'] = count($group['rows']);

            return $group;
        }, $grouped));
    }

    /**
     * @return array<string, mixed>
     */
    private function normaliseRow(object $row): array
    {
        return [
            'queue_id' => $row->queue_id,
            'target_table' => $row->target_table,
            'target_record_kind' => $row->target_record_kind,
            'bronze_uri' => $row->bronze_uri,
            'bronze_row_offset' => $row->bronze_row_offset,
            'payload' => $this->jsonDecode($row->payload),
            'confidence_per_field' => $this->jsonDecode($row->confidence_per_field),
            'confidence_record' => $row->confidence_record !== null ? (float) $row->confidence_record : null,
            'outlier_flags' => $this->jsonDecode($row->outlier_flags),
            'routing_decision' => $row->routing_decision,
            'routing_reason' => $row->routing_reason,
            'lifecycle' => $row->lifecycle,
            'decision_kind' => $row->decision_kind,
            'parser_version' => $row->parser_version,
            'created_at' => (string) $row->created_at,
        ];
    }

    private function jsonDecode($value): mixed
    {
        if ($value === null) {
            return null;
        }
        if (is_array($value)) {
            return $value;
        }
        if (is_string($value)) {
            $decoded = json_decode($value, true);

            return is_array($decoded) ? $decoded : $value;
        }

        return $value;
    }
}
