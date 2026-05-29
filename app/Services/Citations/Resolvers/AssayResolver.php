<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers;

use Illuminate\Http\JsonResponse;
use Illuminate\Support\Facades\DB;

/**
 * Resolves `silver.assays_v2:assay_id=<uuid>` chunk ids to a description
 * of the underlying assay interval. The orchestrator emits this prefix
 * when an answer cites a specific from-to-element grade.
 *
 * Source-id format (matches the orchestrator's _extract_source_id):
 *   silver.assays_v2:assay_id=<uuid>
 *
 * Returned payload:
 *   source_type:     'assays'
 *   source_chunk_id: original prefix passed in (so the inspector card
 *                    can deep-link to the underlying row)
 *   title:           "Au at 145.2-146.1 m in PLS-22-08 — 12.34 g/t"
 *   text:            human-readable one-line summary
 *   metadata:        the full row (element, value, value_ppm, unit,
 *                    from/to depths, lab + certificate, bronze
 *                    provenance link).
 *
 * The metadata.bronze_source_id is the link back to the original lab
 * CSV row — clicking through in the Evidence Inspector should be able
 * to display the raw certificate. The inspector's existing
 * structured_record_lineage path covers that.
 */
final class AssayResolver extends AbstractCitationResolver
{
    public static function prefix(): string
    {
        return 'silver.assays_v2:';
    }

    public function resolve(string $sourceId): JsonResponse
    {
        // Two id shapes are supported:
        //   silver.assays_v2:assay_id=<uuid>
        //   silver.assays_v2:count=N:first=<uuid>  (orchestrator batch pinning)
        $assayId = null;
        if (preg_match('/assay_id=([0-9a-f-]{36})/i', $sourceId, $m)) {
            $assayId = $m[1];
        } elseif (preg_match('/first=([0-9a-f-]{36})/i', $sourceId, $m)) {
            $assayId = $m[1];
        }

        if (! $assayId) {
            return response()->json([
                'source_type' => 'assays',
                'source_chunk_id' => $sourceId,
                'text' => 'Assay query result (no specific interval pinned)',
            ]);
        }

        $row = DB::table('silver.assays_v2 as a')
            ->leftJoin('silver.collars as c', 'c.collar_id', '=', 'a.collar_id')
            ->where('a.id', $assayId)
            ->select([
                'a.id',
                'a.sample_id',
                'a.from_depth',
                'a.to_depth',
                'a.element',
                'a.value',
                'a.value_ppm',
                'a.unit',
                'a.detection_limit',
                'a.over_detection',
                'a.under_detection',
                'a.lab_name',
                'a.certificate_ref',
                'a.analysis_method',
                'a.qaqc_flag',
                'a.bronze_source_id',
                'c.hole_id',
                'c.collar_id',
            ])
            ->first();

        if (! $row) {
            return response()->json([
                'source_type' => 'assays',
                'source_chunk_id' => $sourceId,
                'text' => 'Assay interval not found',
            ]);
        }

        // Format the value as the lab reported it ("12.34 g/t") plus
        // the canonical ppm value for cross-element comparison.
        $valueStr = $this->formatValueWithUnit(
            (float) $row->value,
            (string) $row->unit,
        );

        $title = sprintf(
            '%s at %s–%s m in %s — %s',
            $row->element,
            number_format((float) $row->from_depth, 1),
            number_format((float) $row->to_depth, 1),
            $row->hole_id ?? 'unknown hole',
            $valueStr,
        );

        $text = sprintf(
            '%s assay: %s over %s–%s m (sample %s%s%s)',
            $row->element,
            $valueStr,
            number_format((float) $row->from_depth, 2),
            number_format((float) $row->to_depth, 2),
            $row->sample_id,
            $row->lab_name ? ", lab {$row->lab_name}" : '',
            $row->certificate_ref ? ", cert {$row->certificate_ref}" : '',
        );

        return response()->json([
            'source_type' => 'assays',
            'source_chunk_id' => $sourceId,
            'title' => $title,
            'text' => $text,
            'metadata' => [
                'assay_id' => $row->id,
                'collar_id' => $row->collar_id,
                'hole_id' => $row->hole_id,
                'sample_id' => $row->sample_id,
                'from_depth' => (float) $row->from_depth,
                'to_depth' => (float) $row->to_depth,
                'element' => $row->element,
                'value' => $row->value !== null ? (float) $row->value : null,
                'value_ppm' => $row->value_ppm !== null ? (float) $row->value_ppm : null,
                'unit' => $row->unit,
                'detection_limit' => $row->detection_limit,
                'over_detection' => (bool) $row->over_detection,
                'under_detection' => (bool) $row->under_detection,
                'lab_name' => $row->lab_name,
                'certificate_ref' => $row->certificate_ref,
                'analysis_method' => $row->analysis_method,
                'qaqc_flag' => $row->qaqc_flag,
                // Provenance link — the Evidence Inspector can hop
                // back to bronze.raw_assay_submissions via this UUID.
                'bronze_source_id' => $row->bronze_source_id,
            ],
        ]);
    }

    /**
     * Format a numeric value with its lab-reported unit suitable for
     * display in a citation card. Handles None / detection-limit edge
     * cases by deferring to the caller's metadata field for the full
     * picture.
     */
    private function formatValueWithUnit(?float $value, string $unit): string
    {
        if ($value === null) {
            return "n/a {$unit}";
        }
        // Au and other precious metals at sub-ppm need 3-decimal display;
        // base metals at percent need 2-decimal. Heuristic by unit.
        $decimals = match (true) {
            $unit === 'ppb' => 1,
            $unit === 'ppm' => 2,
            $unit === 'pct' || str_contains($unit, '%') => 2,
            $unit === 'g/t' || $unit === 'oz/t' => 2,
            default => 3,
        };

        return number_format($value, $decimals).' '.$unit;
    }
}
