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
 * Foundry/SourceGraphController — interactive evidence graph.
 *
 * Renders a left-to-right directed graph of provenance:
 *
 *   Section (bronze)     ─┐
 *   Parser (bronze)       ├─→ Report (silver) ─→ Hypothesis (silver)
 *   IngestRun (bronze)   ─┘                       │
 *                                                  └─→ Evidence (link)
 *
 * Project-scoped via:
 *   - bronze.provenance.source_file → PLSS sections used by this project
 *   - silver.reports.project_id     → reports
 *   - silver.collars.project_id     → drillholes
 *   - silver.hypotheses             → workspace-wide (no project_id;
 *     surfaced because reasoning is workspace-scoped today)
 *
 * The React layer uses @xyflow/react to render nodes + edges with
 * zoom/pan/select.
 */
class SourceGraphController extends Controller
{
    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        $workspaceId = $project->workspace_id;

        // ── Column 0 · PLSS sections this project draws from ─────────
        $sections = $this->resolveProjectSections($project->project_id);

        // ── Column 1 · Parsers active on this project ────────────────
        $parsers = DB::select(
            'SELECT bp.parser_name,
                    COUNT(*) AS rows_written
               FROM bronze.provenance bp
               LEFT JOIN silver.collars c ON c.collar_id = bp.target_id AND bp.target_table = \'collars\'
               LEFT JOIN silver.reports r ON r.report_id = bp.target_id AND bp.target_table = \'reports\'
              WHERE (c.project_id = ?::uuid OR r.project_id = ?::uuid)
              GROUP BY bp.parser_name
              ORDER BY rows_written DESC
              LIMIT 6',
            [$project->project_id, $project->project_id],
        );

        // ── Column 2 · Top reports (silver) ──────────────────────────
        $reports = DB::table('silver.reports')
            ->where('project_id', $project->project_id)
            ->whereNotNull('sections_text')
            ->select('report_id', 'title', 'company')
            ->orderByDesc('updated_at')
            ->limit(6)
            ->get();
        $reportsCount = (int) DB::table('silver.reports')
            ->where('project_id', $project->project_id)
            ->count();

        // ── Column 2b · Drillholes (silver.collars summary) ──────────
        $collarsCount = (int) DB::table('silver.collars')
            ->where('project_id', $project->project_id)
            ->count();

        // ── Column 3 · Hypotheses (silver, workspace-scoped) ─────────
        $hypotheses = DB::table('silver.hypotheses')
            ->where('workspace_id', $workspaceId)
            ->select('hypothesis_id', 'parent_question', 'label', 'confidence', 'review_status')
            ->orderByDesc('confidence')
            ->orderByDesc('created_at')
            ->limit(9)
            ->get();

        // ── Column 4 · Evidence link rollup per hypothesis ───────────
        $hypIds = $hypotheses->pluck('hypothesis_id')->all();
        $linkRollups = collect();
        if (! empty($hypIds)) {
            $linkRollups = DB::table('silver.hypothesis_evidence_links')
                ->select('hypothesis_id', 'role', DB::raw('COUNT(*) AS n'))
                ->whereIn('hypothesis_id', $hypIds)
                ->where('workspace_id', $workspaceId)
                ->groupBy('hypothesis_id', 'role')
                ->get();
        }

        // ── Build node + edge lists ──────────────────────────────────
        $nodes = [];
        $edges = [];

        // Column 0: sections
        foreach ($sections as $i => $section) {
            $nodes[] = [
                'id'    => "sec-{$section}",
                'col'   => 0,
                'kind'  => 'section',
                'label' => "PLSS {$section}",
                'meta'  => 'bronze cluster',
            ];
        }
        // Fallback section node if no provenance yet
        if (empty($sections)) {
            $nodes[] = [
                'id'    => 'sec-none',
                'col'   => 0,
                'kind'  => 'section',
                'label' => 'No bronze yet',
                'meta'  => 'connect a source',
            ];
        }

        // Column 1: parsers
        foreach ($parsers as $i => $p) {
            $nodeId = "parser-{$p->parser_name}";
            $nodes[] = [
                'id'    => $nodeId,
                'col'   => 1,
                'kind'  => 'parser',
                'label' => $p->parser_name,
                'meta'  => "{$p->rows_written} rows",
            ];
            // edge from every section → this parser
            foreach ($sections as $section) {
                $edges[] = [
                    'id'     => "sec-{$section}->{$nodeId}",
                    'source' => "sec-{$section}",
                    'target' => $nodeId,
                ];
            }
        }

        // Column 2: documents (reports + a drillhole-summary node)
        $collarsNode = 'collars-summary';
        $nodes[] = [
            'id'    => $collarsNode,
            'col'   => 2,
            'kind'  => 'collars',
            'label' => "{$collarsCount} drillholes",
            'meta'  => 'silver.collars',
        ];
        foreach ($parsers as $p) {
            $edges[] = [
                'id'     => "parser-{$p->parser_name}->{$collarsNode}",
                'source' => "parser-{$p->parser_name}",
                'target' => $collarsNode,
            ];
        }
        foreach ($reports as $r) {
            $nodeId = "rep-{$r->report_id}";
            $nodes[] = [
                'id'         => $nodeId,
                'col'        => 2,
                'kind'       => 'report',
                'label'      => mb_substr((string) ($r->title ?? '—'), 0, 40),
                'meta'       => (string) ($r->company ?? ''),
                'report_id'  => (string) $r->report_id,
            ];
            // Connect every parser → every featured report (visual; real
            // 1:1 mapping would need passage-level provenance fanout)
            foreach ($parsers as $p) {
                $edges[] = [
                    'id'     => "parser-{$p->parser_name}->{$nodeId}",
                    'source' => "parser-{$p->parser_name}",
                    'target' => $nodeId,
                ];
            }
        }
        // Summary "+N more" node when there are extra reports
        if ($reportsCount > count($reports)) {
            $more = $reportsCount - count($reports);
            $nodes[] = [
                'id'    => 'rep-more',
                'col'   => 2,
                'kind'  => 'report-summary',
                'label' => "+{$more} more reports",
                'meta'  => 'silver.reports',
            ];
            foreach ($parsers as $p) {
                $edges[] = [
                    'id'     => "parser-{$p->parser_name}->rep-more",
                    'source' => "parser-{$p->parser_name}",
                    'target' => 'rep-more',
                ];
            }
        }

        // Column 3: hypotheses
        $rollByHypId = $linkRollups->groupBy('hypothesis_id');
        foreach ($hypotheses as $h) {
            $byRole = collect($rollByHypId->get($h->hypothesis_id, []))
                ->keyBy('role')
                ->map(fn ($r) => (int) $r->n);
            $support = $byRole->get('supporting', 0);
            $contra  = $byRole->get('contradicting', 0);

            $nodeId = "hyp-{$h->hypothesis_id}";
            $nodes[] = [
                'id'            => $nodeId,
                'col'           => 3,
                'kind'          => 'hypothesis',
                'label'         => mb_substr((string) ($h->parent_question ?? ''), 0, 60),
                'meta'          => sprintf('%s · %s · conf %s',
                    $h->label ?? '?',
                    $h->review_status ?? 'ai_suggested',
                    $h->confidence !== null ? number_format((float) $h->confidence, 2) : '—',
                ),
                'support_count'    => $support,
                'contradict_count' => $contra,
                'hypothesis_id'    => (string) $h->hypothesis_id,
            ];

            // Connect every document-column node → this hypothesis
            foreach ($reports as $r) {
                $edges[] = [
                    'id'     => "rep-{$r->report_id}->{$nodeId}",
                    'source' => "rep-{$r->report_id}",
                    'target' => $nodeId,
                ];
            }
            $edges[] = [
                'id'     => "{$collarsNode}->{$nodeId}",
                'source' => $collarsNode,
                'target' => $nodeId,
            ];
        }

        // Column 4: evidence summary node
        $totalLinks = $linkRollups->sum('n');
        if ($totalLinks > 0) {
            $evNode = 'evidence-summary';
            $nodes[] = [
                'id'    => $evNode,
                'col'   => 4,
                'kind'  => 'evidence',
                'label' => "{$totalLinks} evidence links",
                'meta'  => 'hypothesis_evidence_links',
            ];
            foreach ($hypotheses as $h) {
                $edges[] = [
                    'id'     => "hyp-{$h->hypothesis_id}->{$evNode}",
                    'source' => "hyp-{$h->hypothesis_id}",
                    'target' => $evNode,
                ];
            }
        }

        // De-dup edges
        $edges = array_values(array_reduce($edges, function ($carry, $e) {
            $carry[$e['id']] = $e;
            return $carry;
        }, []));

        return Inertia::render('Foundry/SourceGraph', [
            'project' => [
                'project_id'   => $project->project_id,
                'project_name' => $project->project_name,
                'slug'         => $project->slug,
            ],
            'nodes'    => $nodes,
            'edges'    => $edges,
            'stats'    => [
                'sections'         => count($sections),
                'parsers'          => count($parsers),
                'reports'          => $reportsCount,
                'reports_featured' => count($reports),
                'collars'          => $collarsCount,
                'hypotheses'       => count($hypotheses),
                'evidence_links'   => (int) $totalLinks,
            ],
            'empty'    => empty($sections) && $reportsCount === 0 && empty($hypotheses),
        ]);
    }

    /**
     * @return list<string>
     */
    private function resolveProjectSections(string $projectId): array
    {
        $rows = DB::select(
            "SELECT DISTINCT
                substring(bp.source_file FROM '(?:extract|data)/([0-9]{3}N[0-9]{3}W[0-9A-Z]+)/') AS section
              FROM bronze.provenance bp
              LEFT JOIN silver.collars c ON c.collar_id = bp.target_id AND bp.target_table = 'collars'
              LEFT JOIN silver.reports r ON r.report_id = bp.target_id AND bp.target_table = 'reports'
              WHERE c.project_id = ?::uuid OR r.project_id = ?::uuid",
            [$projectId, $projectId],
        );
        $sections = [];
        foreach ($rows as $row) {
            if (! empty($row->section)) {
                $sections[] = (string) $row->section;
            }
        }
        return array_values(array_unique($sections));
    }
}
