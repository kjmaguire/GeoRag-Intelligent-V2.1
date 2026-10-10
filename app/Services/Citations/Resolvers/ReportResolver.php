<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers;

use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;

/**
 * Resolves `georag_reports:{report_id}:section={n}:chunk={passage_id}` chunk
 * ids to the passage the answer actually cited.
 *
 * Returns:
 *   - The cited passage's own text and pages, read from
 *     silver.document_passages by `chunk=` (the Qdrant point id, which is the
 *     passage id). This is what the Evidence Inspector promises: the exact
 *     passage text.
 *   - For an id with no usable `chunk=` (older citations), the named section
 *     of `silver.reports.sections_text`, or the first 1–2 sections as a
 *     fallback excerpt, so the viewer always shows *something*.
 *   - A structured "Report not found" 404 when the report_id does not
 *     resolve inside the caller-verified workspace — deliberately identical
 *     for "does not exist" and "belongs to another tenant" (no existence
 *     oracle; security fix 2026-08-14).
 *
 * `section=` is NOT a section number for passage citations: the embedder
 * writes the passage ordinal there (passage_embedder.py, `section_number =
 * str(row["ordinal"])`). Looking that up in sections_text — which is keyed
 * by NI 43-101 section — showed an unrelated section, labelled as the source.
 *
 * `georag_reports:None:...` is an ADR-0012 structured summary: a passage with
 * no parent report, scoped by its own project_id. It resolves by chunk alone.
 */
final class ReportResolver extends AbstractCitationResolver
{
    /**
     * The report_id slot of a passage that has no parent report.
     */
    private const NO_REPORT = 'None';

    public static function prefix(): string
    {
        return 'georag_reports:';
    }

    /**
     * @param list<string>|null $projectIds
     */
    public function resolve(string $sourceId, ?string $workspaceId = null, ?array $projectIds = null): JsonResponse
    {
        // Parse: georag_reports:{report_id}:section={n}:chunk={passage_id}
        preg_match('/georag_reports:([^:]+)/', $sourceId, $matches);
        $reportId = $matches[1] ?? null;

        preg_match('/section=([^:]+)/', $sourceId, $sectionMatch);
        $sectionNum = $sectionMatch[1] ?? null;

        preg_match('/chunk=([^:]+)/', $sourceId, $chunkMatch);
        $chunkId = isset($chunkMatch[1]) && Str::isUuid($chunkMatch[1]) ? $chunkMatch[1] : null;

        if (! $reportId) {
            return response()->json([
                'source_type' => 'report',
                'text' => 'Report ID not found in source_chunk_id',
            ]);
        }

        // Belt and braces: the controller already binds the app.workspace_id
        // GUC for RLS, but silver RLS policies are fail-open when the GUC is
        // unset — so ALSO filter explicitly. A null scope fails CLOSED.
        if ($workspaceId === null || $projectIds === null || $projectIds === []) {
            return $this->notFound($sourceId);
        }

        // report_id is a uuid column: anything else would be a Postgres
        // 22P02 and a 500, where "not found" is the answer.
        $hasReport = Str::isUuid($reportId);
        if (! $hasReport && $reportId !== self::NO_REPORT) {
            return $this->notFound($sourceId);
        }

        $passage = $chunkId === null
            ? null
            : $this->loadPassage($chunkId, $hasReport ? $reportId : null, $workspaceId, $projectIds);

        if (! $hasReport) {
            // A structured summary has no report to fall back to.
            if ($passage === null) {
                return $this->notFound($sourceId);
            }

            return response()->json([
                'source_type' => 'report',
                'source_chunk_id' => $sourceId,
                'title' => 'Structured data summary',
                'section_title' => $this->passageLabel($passage),
                'section_number' => null,
                'text' => (string) $passage->text,
                'metadata' => [
                    'passage_id' => $passage->passage_id,
                ],
            ]);
        }

        $report = DB::table('silver.reports')
            ->where('report_id', $reportId)
            ->where('workspace_id', $workspaceId)
            ->whereIn('project_id', $projectIds)
            ->first(['report_id', 'title', 'company', 'filing_date', 'commodity', 'sections_text']);

        if (! $report) {
            return $this->notFound($sourceId);
        }

        $metadata = [
            'report_id' => $report->report_id,
            'company' => $report->company,
            'filing_date' => $report->filing_date,
            'commodity' => $report->commodity,
        ];

        if ($passage !== null) {
            $sectionText = (string) $passage->text;
            $sectionTitle = $this->passageLabel($passage);
            // Not a sections_text key, so the Reader must not be told to
            // highlight a section by it.
            $sectionNum = null;
            $metadata['passage_id'] = $passage->passage_id;
            $metadata['page_first'] = $passage->page_first;
            $metadata['page_last'] = $passage->page_last;
        } else {
            $sections = json_decode((string) $report->sections_text, true) ?? [];

            if ($sectionNum !== null && isset($sections[$sectionNum])) {
                $sectionText = (string) $sections[$sectionNum];
                $sectionTitle = "Section {$sectionNum}";
            } else {
                // Fallback excerpt — first 1–2 sections.
                $sectionText = implode("\n\n", array_slice($sections, 0, 2));
                $sectionTitle = 'Report excerpt';
            }
        }

        return response()->json([
            'source_type' => 'report',
            'source_chunk_id' => $sourceId,
            'title' => $report->title,
            'section_title' => $sectionTitle,
            'section_number' => $sectionNum,
            'text' => $sectionText,
            'metadata' => $metadata,
            // Cross-corpus linker — inverse view (plan §07d). Empty state
            // stays clean when no SMAD-style references have been extracted.
            'references_to_entities' => $this->loadDocumentReferencesSummary($report->report_id),
        ]);
    }

    /**
     * The cited passage, scoped like the report it belongs to.
     *
     * A report-derived passage takes its project from its report; a
     * structured summary (no report) carries its own project_id. Either way
     * the project must be one the caller may read, and the workspace must be
     * the caller's — the same rule as the report lookup below.
     *
     * The id is matched against both passage_id and embedding_id: the Qdrant
     * point id IS the passage id (passage_embedder._passage_to_point_id), and
     * embedding_id records the point id the row was written under.
     *
     * @param list<string> $projectIds
     */
    private function loadPassage(string $chunkId, ?string $reportId, string $workspaceId, array $projectIds): ?object
    {
        $query = DB::table('silver.document_passages as dp')
            ->where('dp.workspace_id', $workspaceId)
            ->where(function ($q) use ($chunkId): void {
                $q->where('dp.passage_id', $chunkId)->orWhere('dp.embedding_id', $chunkId);
            });

        if ($reportId === null) {
            $query->whereNull('dp.document_id')->whereIn('dp.project_id', $projectIds);
        } else {
            $query->where('dp.document_id', $reportId)
                ->whereExists(function ($q) use ($projectIds): void {
                    $q->select(DB::raw(1))
                        ->from('silver.reports as r')
                        ->whereColumn('r.report_id', 'dp.document_id')
                        ->whereIn('r.project_id', $projectIds);
                });
        }

        return $query->first(['dp.passage_id', 'dp.ordinal', 'dp.text', 'dp.page_first', 'dp.page_last']);
    }

    /**
     * "Passage 12 (pp. 41–42)" — what the citation points at, without
     * pretending the ordinal is an NI 43-101 section number.
     */
    private function passageLabel(object $passage): string
    {
        $label = 'Passage '.((int) $passage->ordinal + 1);

        $first = $passage->page_first;
        $last = $passage->page_last;
        if ($first !== null && $last !== null && (int) $last !== (int) $first) {
            return "{$label} (pp. {$first}–{$last})";
        }
        if ($first !== null) {
            return "{$label} (p. {$first})";
        }

        return $label;
    }

    /**
     * Structured 404 for a report that is not visible in the caller's
     * workspace — same body shape the viewer already renders, same response
     * whether the report is missing or cross-tenant.
     */
    private function notFound(string $sourceId): JsonResponse
    {
        return response()->json([
            'source_type' => 'report',
            'source_chunk_id' => $sourceId,
            'text' => 'Report not found in database',
        ], 404);
    }

    /**
     * Count active `(:Document)-[:REFERENCES]->(:Entity)` links originating
     * from one document, grouped by canonical_type so the document card can
     * render "References N mines / M occurrences / K drillholes".
     *
     * Returns zero-filled counts so the frontend can always rely on the shape.
     *
     * @return array{total: int, by_canonical_type: array<string, int>, entities: array<int, array<string, mixed>>}
     */
    private function loadDocumentReferencesSummary(?string $reportId): array
    {
        $zero = [
            'total' => 0,
            'by_canonical_type' => [
                'mine' => 0,
                'mineral_occurrence' => 0,
                'drillhole_collar' => 0,
                'resource_potential_zone' => 0,
            ],
            'entities' => [],
        ];

        if ($reportId === null) {
            return $zero;
        }

        $rows = DB::table('public_geo.document_entity_links')
            ->where('document_id', $reportId)
            ->whereNull('superseded_at')
            ->select(['canonical_type', DB::raw('COUNT(*) as count')])
            ->groupBy('canonical_type')
            ->get();

        if ($rows->isEmpty()) {
            return $zero;
        }

        $by = $zero['by_canonical_type'];
        $total = 0;
        foreach ($rows as $row) {
            $c = (int) $row->count;
            $by[$row->canonical_type] = $c;
            $total += $c;
        }

        // Preview: up to 10 top-scoring entities, oldest established first
        // so re-verdicts don't reshuffle the UI unless they materially change
        // the set.
        $preview = DB::table('public_geo.document_entity_links as l')
            ->where('l.document_id', $reportId)
            ->whereNull('l.superseded_at')
            ->orderByDesc('l.confidence')
            ->orderBy('l.established_at')
            ->limit(10)
            ->get(['l.canonical_type', 'l.entity_id', 'l.confidence', 'l.signals']);

        return [
            'total' => $total,
            'by_canonical_type' => $by,
            'entities' => $preview->map(fn ($r) => [
                'canonical_type' => $r->canonical_type,
                'entity_id' => $r->entity_id,
                'confidence' => (float) $r->confidence,
                'signals' => $this->decodeSignals($r->signals),
            ])->all(),
        ];
    }
}
