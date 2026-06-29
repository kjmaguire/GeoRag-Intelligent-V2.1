<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Services\FastApiJwtMinter;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/DrillholeDetailController — §5.12 anchored-scroll per-hole page.
 *
 * Reads gold.drillhole_intervals_visual + gold.structure_measurements_visual
 * + gold.cross_section_panels + silver.collars + silver.assays_v2 to render
 * the four anchored sections defined in the master plan:
 *
 *     Header (sticky)    — collar metadata
 *     Strip Log          — gold.drillhole_intervals_visual rows
 *     Assays             — silver.assays_v2 highlights
 *     Structures         — gold.structure_measurements_visual (stereonet pre-projected)
 *     Cross Section      — gold.cross_section_panels intersecting this hole
 *
 * Falls back gracefully when a gold table is empty (renders the section
 * with an "agent has not yet generated this visual" message) so users can
 * land on the page before the gold pipeline has fired for their hole.
 */
class DrillholeDetailController extends Controller
{
    use SetsWorkspaceRlsContext;

    public function show(Request $request, string $slug, string $collarId): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        // Pin the RLS GUC so per-collar queries on silver / gold are tenant-scoped.
        $workspaceId = (string) DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->value('workspace_id');

        return $this->withWorkspaceRls($workspaceId, function () use ($request, $project, $collarId) {
            $collar = DB::table('silver.collars')
                ->where('collar_id', $collarId)
                ->where('project_id', $project->project_id)
                ->first();

            if ($collar === null) {
                abort(404);
            }

            $intervals = $this->safeQuery(
                fn () => DB::table('gold.drillhole_intervals_visual')
                    ->where('collar_id', $collarId)
                    ->orderBy('depth_from')
                    ->get(),
            );

            $assayHighlights = $this->safeQuery(
                fn () => DB::table('silver.assays_v2')
                    ->where('collar_id', $collarId)
                    ->orderByDesc('value_ppm')
                    ->limit(20)
                    ->get(),
            );

            $structures = $this->safeQuery(
                fn () => DB::table('gold.structure_measurements_visual')
                    ->where('collar_id', $collarId)
                    ->orderBy('depth')
                    ->get(),
            );

            // JSONB containment via the holes[] array — index-friendly under a
            // GIN(panel_payload jsonb_path_ops) once large enough to matter.
            // Beats `panel_payload::text ILIKE '%uuid%'` which forces a full
            // sequential scan + stringification on every row.
            $crossSections = $this->safeQuery(
                fn () => DB::table('gold.cross_section_panels')
                    ->where('project_id', $project->project_id)
                    ->whereRaw(
                        "panel_payload -> 'holes' @> ?::jsonb",
                        [json_encode([['collar_id' => $collarId]])],
                    )
                    ->orderBy('section_name')
                    ->get(),
            );

            $qa = $this->fetchVisualQa($request, $project->project_id, $collarId);
            $lithologyQuality = $this->lithologyQualityCounters($collarId);
            $dqFlags = $this->dataQualityFlagSummary($collarId);

            return Inertia::render('Foundry/DrillholeDetail', [
                'project' => [
                    'project_id' => $project->project_id,
                    'project_name' => $project->project_name,
                    'slug' => $project->slug,
                ],
                'collar' => $collar,
                'intervals' => $intervals,
                'assays' => $assayHighlights,
                'structures' => $structures,
                'cross_sections' => $crossSections,
                'qa' => $qa,
                'lithology_quality' => $lithologyQuality,
                'data_quality_flags' => $dqFlags,
            ]);
        });
    }

    /**
     * Plan §6a — per-collar data-quality flag summary for the badge UI.
     *
     * Returns an array shape the DataQualityFlagsBadge React component
     * consumes::
     *
     *     [
     *       'counts' => ['ERROR' => 0, 'WARNING' => 1, 'INFO' => 2],
     *       'open_total' => 3,
     *       'flags' => [
     *         ['flag_type' => 'collar.missing_elevation',
     *          'severity'  => 'WARNING',
     *          'description' => 'Collar ECK-22-001: elevation is NULL ...',
     *          'flagged_at' => '2026-05-29T12:34:56+00:00'],
     *         ...
     *       ],
     *     ]
     *
     * The flag rows are scoped via the workspace_id GUC set in show();
     * RLS guarantees we don't surface flags from another workspace
     * even if the controller is reused with a different slug.
     *
     * @return array{counts: array<string, int>, open_total: int, flags: array<int, array<string, mixed>>}
     */
    private function dataQualityFlagSummary(string $collarId): array
    {
        // Counts by severity for the badge dots.
        $countRows = DB::table('silver.data_quality_flags')
            ->where('record_type', 'collar')
            ->where('record_id', $collarId)
            ->whereNull('resolved_at')
            ->select('severity', DB::raw('count(*)::int as n'))
            ->groupBy('severity')
            ->get();

        $counts = ['ERROR' => 0, 'WARNING' => 0, 'INFO' => 0];
        foreach ($countRows as $row) {
            if (isset($counts[$row->severity])) {
                $counts[$row->severity] = (int) $row->n;
            }
        }

        // Cap the rendered flag list — a verbose collar can produce
        // dozens of flags. The badge popover shows ERROR first,
        // then WARNING, then INFO, latest-first within each tier.
        $flags = DB::table('silver.data_quality_flags')
            ->where('record_type', 'collar')
            ->where('record_id', $collarId)
            ->whereNull('resolved_at')
            ->orderByRaw("CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARNING' THEN 1 ELSE 2 END")
            ->orderByDesc('flagged_at')
            ->limit(20)
            ->select(
                'flag_id',
                'flag_type',
                'severity',
                'description',
                'rule_id',
                'rule_version',
                'flagged_at',
            )
            ->get()
            ->map(fn ($f) => [
                'flag_id' => $f->flag_id,
                'flag_type' => $f->flag_type,
                'severity' => $f->severity,
                'description' => $f->description,
                'rule_id' => $f->rule_id,
                'rule_version' => $f->rule_version,
                'flagged_at' => $f->flagged_at,
            ])
            ->all();

        return [
            'counts' => $counts,
            'open_total' => array_sum($counts),
            'flags' => $flags,
        ];
    }

    /**
     * CC-02 Item 3 — per-hole lithology resolution quality counters.
     *
     * Reads silver.lithology (the new dual-system rock_code table) and
     * returns the per-bucket row count so the DataQualityBadge can show
     * geologists which holes have unresolved or low-confidence lithology
     * rows that need review.
     *
     * Buckets:
     *   exact            — rock_code_confidence = 1.0
     *   fuzzy            — rock_code_confidence in [0, 1)  (rapidfuzz match)
     *   unmapped         — rock_code IS NULL (catalogue gap)
     *
     * Returns null when silver.lithology has zero rows for this collar
     * so the page can hide the badge entirely rather than render
     * "0 / 0 mapped" which is meaningless.
     *
     * @return array{exact:int, fuzzy:int, unmapped:int, total:int}|null
     */
    private function lithologyQualityCounters(string $collarId): ?array
    {
        try {
            $row = DB::table('silver.lithology')
                ->where('collar_id', $collarId)
                ->selectRaw(
                    'COUNT(*) FILTER (WHERE rock_code_confidence = 1.0) AS exact, '
                    .'COUNT(*) FILTER (WHERE rock_code_confidence IS NOT NULL AND rock_code_confidence < 1.0) AS fuzzy, '
                    .'COUNT(*) FILTER (WHERE rock_code IS NULL) AS unmapped, '
                    .'COUNT(*) AS total',
                )
                ->first();

            if ($row === null || (int) $row->total === 0) {
                return null;
            }

            return [
                'exact' => (int) $row->exact,
                'fuzzy' => (int) $row->fuzzy,
                'unmapped' => (int) $row->unmapped,
                'total' => (int) $row->total,
            ];
        } catch (\Throwable $e) {
            return null;
        }
    }

    /**
     * Call FastAPI POST /v1/viz/qa for this hole. Returns null on any
     * failure (page still renders without the QA banner).
     *
     * @return array<string, mixed>|null
     */
    private function fetchVisualQa(Request $request, string $projectId, string $collarId): ?array
    {
        try {
            $fastApiBase = rtrim(
                (string) (config('services.fastapi.internal_url')
                    ?? config('services.fastapi.internal_url')),
                '/',
            );
            $serviceKey = config('services.fastapi.service_key') ?? config('services.fastapi.service_key');
            if (! $serviceKey) {
                return null;
            }

            $jwt = app(FastApiJwtMinter::class)->mint(
                (string) $request->user()->id,
                $projectId,
                [],
            );

            $resp = Http::withHeaders([
                'X-Service-Key' => $serviceKey,
                'Authorization' => 'Bearer '.$jwt,
                'Accept' => 'application/json',
            ])->timeout(5)->post($fastApiBase.'/v1/viz/qa', [
                'collar_id' => $collarId,
            ]);

            if (! $resp->ok()) {
                return null;
            }

            return $resp->json();
        } catch (\Throwable $e) {
            return null;
        }
    }

    private function safeQuery(\Closure $fn): array
    {
        try {
            return $fn()->map(fn ($row) => (array) $row)->all();
        } catch (\Throwable $e) {
            return [];
        }
    }
}
