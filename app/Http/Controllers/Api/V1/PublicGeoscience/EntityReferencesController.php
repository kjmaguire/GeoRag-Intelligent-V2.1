<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1\PublicGeoscience;

use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

/**
 * Cross-corpus drill-in endpoint — full list of documents that reference one
 * Public Geoscience entity, or the full list of entities referenced by one
 * document (plan §07d drill-in affordances).
 *
 * The citation-resolver payload returns up to 5/10 rows for at-a-glance
 * rendering; when the user clicks "Referenced in N reports" on an entity
 * card (or "References N mines" on a document card), this endpoint returns
 * the complete list with one row per active link.
 *
 * Routes:
 *   GET /api/v1/public-geoscience/entities/{canonical_type}/{pg_id}/references
 *   GET /api/v1/public-geoscience/documents/{report_id}/references
 */
class EntityReferencesController extends Controller
{
    private const VALID_CANONICAL_TYPES = [
        'mine',
        'mineral_occurrence',
        'drillhole_collar',
        'resource_potential_zone',
    ];

    /**
     * All active document→entity links for a given PG entity.
     */
    public function forEntity(Request $request, string $canonicalType, string $pgId): JsonResponse
    {
        if (! in_array($canonicalType, self::VALID_CANONICAL_TYPES, true)) {
            return response()->json(
                ['message' => "Unknown canonical_type '{$canonicalType}'."],
                404,
            );
        }

        // Guard against malformed UUIDs reaching the DB.
        if (! preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i', $pgId)) {
            return response()->json(['message' => 'Invalid pg_id UUID.'], 400);
        }

        $minConfidence = (float) $request->query('min_confidence', 0.6);

        $rows = DB::table('public_geo.document_entity_links as l')
            ->leftJoin('silver.reports as r', 'r.report_id', '=', 'l.document_id')
            ->where('l.entity_id', $pgId)
            ->where('l.canonical_type', $canonicalType)
            ->where('l.confidence', '>=', $minConfidence)
            ->whereNull('l.superseded_at')
            ->orderByDesc('l.established_at')
            ->get([
                'l.document_id',
                'l.document_filename',
                'l.confidence',
                'l.signals',
                'l.extracted_context',
                'l.established_at',
                'l.established_by',
                'r.title',
                'r.filing_date',
                'r.company',
                'r.commodity',
            ]);

        return response()->json([
            'canonical_type' => $canonicalType,
            'pg_id' => $pgId,
            'total' => $rows->count(),
            'min_confidence' => $minConfidence,
            'documents' => $rows->map(function ($r) {
                return [
                    'document_id' => $r->document_id,
                    'title' => $r->title,
                    'filename' => $r->document_filename,
                    'filing_date' => $r->filing_date,
                    'company' => $r->company,
                    'commodity' => $r->commodity,
                    'confidence' => (float) $r->confidence,
                    'signals' => $this->decodeSignals($r->signals),
                    'extracted_context' => $r->extracted_context,
                    'established_at' => $r->established_at,
                    'established_by' => $r->established_by,
                ];
            })->all(),
        ]);
    }

    /**
     * All active entity→document links originating from a given document,
     * grouped by canonical_type so the UI can render per-type sections
     * (plan §07d: "N mines, M occurrences, K drillholes, J resource potential
     * zones").
     */
    public function forDocument(Request $request, string $reportId): JsonResponse
    {
        if (! preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i', $reportId)) {
            return response()->json(['message' => 'Invalid report_id UUID.'], 400);
        }

        $minConfidence = (float) $request->query('min_confidence', 0.6);

        $rows = DB::table('public_geo.document_entity_links')
            ->where('document_id', $reportId)
            ->where('confidence', '>=', $minConfidence)
            ->whereNull('superseded_at')
            ->orderBy('canonical_type')
            ->orderByDesc('confidence')
            ->get([
                'canonical_type',
                'entity_id',
                'confidence',
                'signals',
                'extracted_context',
                'established_at',
                'established_by',
            ]);

        // Hydrate entity display name per canonical_type. One fetch per type.
        $byType = $rows->groupBy('canonical_type');
        $hydrated = [];
        foreach ($byType as $canonicalType => $links) {
            $entityIds = $links->pluck('entity_id')->unique()->all();
            $names = $this->fetchEntityNames($canonicalType, $entityIds);

            $hydrated[$canonicalType] = $links->map(function ($link) use ($names) {
                $display = $names[$link->entity_id] ?? null;

                return [
                    'entity_id' => $link->entity_id,
                    'display_name' => $display['name'] ?? null,
                    'jurisdiction_code' => $display['jurisdiction_code'] ?? null,
                    'source_id' => $display['source_id'] ?? null,
                    'confidence' => (float) $link->confidence,
                    'signals' => $this->decodeSignals($link->signals),
                    'extracted_context' => $link->extracted_context,
                    'established_at' => $link->established_at,
                    'established_by' => $link->established_by,
                ];
            })->values()->all();
        }

        // Counts include zeros for all canonical types so the UI can render
        // a consistent shape.
        $counts = [
            'mine' => 0,
            'mineral_occurrence' => 0,
            'drillhole_collar' => 0,
            'resource_potential_zone' => 0,
        ];
        foreach ($byType as $type => $group) {
            $counts[$type] = $group->count();
        }

        return response()->json([
            'document_id' => $reportId,
            'total' => $rows->count(),
            'min_confidence' => $minConfidence,
            'counts' => $counts,
            'by_canonical_type' => $hydrated,
        ]);
    }

    /**
     * Fetch display names for a batch of entity IDs within one canonical_type.
     */
    private function fetchEntityNames(string $canonicalType, array $entityIds): array
    {
        if (empty($entityIds)) {
            return [];
        }

        $table = match ($canonicalType) {
            'mine' => 'public_geo.pg_mine',
            'mineral_occurrence' => 'public_geo.pg_mineral_occurrence',
            'drillhole_collar' => 'public_geo.pg_drillhole_collar',
            'resource_potential_zone' => 'public_geo.pg_resource_potential_zone',
            'rock_sample' => 'public_geo.pg_rock_sample',
            'assessment_survey' => 'public_geo.pg_assessment_survey',
            'mineral_disposition' => 'public_geo.pg_mineral_disposition',
            default => null,
        };
        if ($table === null) {
            return [];
        }

        $nameExpr = match ($canonicalType) {
            'drillhole_collar' => DB::raw('COALESCE(drillhole_name, drillhole_id) AS name'),
            'resource_potential_zone' => DB::raw('commodity AS name'),
            'rock_sample' => DB::raw('COALESCE(sample_number, station) AS name'),
            'assessment_survey' => DB::raw("survey_type || ' survey' AS name"),
            'mineral_disposition' => DB::raw('disposition_number AS name'),
            default => 'name',
        };

        $rows = DB::table($table)
            ->whereIn('id', $entityIds)
            ->select(['id', $nameExpr, 'jurisdiction_code', 'source_id'])
            ->get();

        $out = [];
        foreach ($rows as $row) {
            $out[$row->id] = [
                'name' => $row->name,
                'jurisdiction_code' => $row->jurisdiction_code,
                'source_id' => $row->source_id,
            ];
        }

        return $out;
    }

    private function decodeSignals(mixed $value): array
    {
        if ($value === null) {
            return [];
        }
        if (is_array($value)) {
            return $value;
        }
        $decoded = json_decode((string) $value, true);

        return is_array($decoded) ? $decoded : [];
    }
}
