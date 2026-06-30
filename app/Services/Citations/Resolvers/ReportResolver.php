<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers;

use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\DB;

/**
 * Resolves `georag_reports:{report_id}:section={num}:chunk={id}` chunk ids
 * to the underlying NI 43-101 report section text.
 *
 * Returns:
 *   - The named section's text when `section=N` resolves cleanly inside
 *     `silver.reports.sections_text`.
 *   - The first 1–2 sections as a fallback excerpt when the section number
 *     is missing or out of range (citation viewer always shows *something*
 *     rather than blank).
 *   - A "Report not found" message when the report_id doesn't exist —
 *     intentional 200 with body explanation, not 404 (the citation viewer
 *     surfaces the gap to the user; an HTTP 404 would be invisible).
 */
final class ReportResolver extends AbstractCitationResolver
{
    public static function prefix(): string
    {
        return 'georag_reports:';
    }

    public function resolve(string $sourceId): JsonResponse
    {
        // Parse: georag_reports:{report_id}:section={num}:chunk={id}
        preg_match('/georag_reports:([^:]+)/', $sourceId, $matches);
        $reportId = $matches[1] ?? null;

        preg_match('/section=([^:]+)/', $sourceId, $sectionMatch);
        $sectionNum = $sectionMatch[1] ?? null;

        if (! $reportId) {
            return response()->json([
                'source_type' => 'report',
                'text' => 'Report ID not found in source_chunk_id',
            ]);
        }

        $report = DB::table('silver.reports')
            ->where('report_id', $reportId)
            ->first(['report_id', 'title', 'company', 'filing_date', 'commodity', 'sections_text']);

        if (! $report) {
            return response()->json([
                'source_type' => 'report',
                'text' => 'Report not found in database',
            ]);
        }

        $sections = json_decode((string) $report->sections_text, true) ?? [];
        $sectionText = '';
        $sectionTitle = '';

        if ($sectionNum !== null && isset($sections[$sectionNum])) {
            $sectionText = (string) $sections[$sectionNum];
            $sectionTitle = "Section {$sectionNum}";
        } else {
            // Fallback excerpt — first 1–2 sections.
            $sectionText = implode("\n\n", array_slice($sections, 0, 2));
            $sectionTitle = 'Report excerpt';
        }

        return response()->json([
            'source_type' => 'report',
            'source_chunk_id' => $sourceId,
            'title' => $report->title,
            'section_title' => $sectionTitle,
            'section_number' => $sectionNum,
            'text' => $sectionText,
            'metadata' => [
                'report_id' => $report->report_id,
                'company' => $report->company,
                'filing_date' => $report->filing_date,
                'commodity' => $report->commodity,
            ],
            // Cross-corpus linker — inverse view (plan §07d). Empty state
            // stays clean when no SMAD-style references have been extracted.
            'references_to_entities' => $this->loadDocumentReferencesSummary($report->report_id),
        ]);
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
