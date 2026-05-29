<?php

declare(strict_types=1);

namespace App\Services\Exports;

use App\Models\Collar;
use Illuminate\Support\Collection;
use Illuminate\Support\Facades\DB;

/**
 * CC-01 Item 6 — DXF exporter (CAD-compatible drillhole collar layer).
 *
 * Emits AutoCAD-2018 DXF (AC1027) ASCII directly. No ezdxf dependency
 * — the DXF format is a well-documented ASCII schema and a POINT-only
 * collar layer is a few hundred lines of templated text. Avoids
 * rebuilding the FastAPI container just for one exporter.
 *
 * Output shape:
 *   - HEADER section with $ACADVER=AC1027 and $INSUNITS=6 (metres).
 *   - ENTITIES section with one POINT entity per collar, on layer
 *     "GEORAG_COLLARS". Each POINT carries a TEXT entity sibling with
 *     the hole_id annotation, offset 5 m east of the point.
 *
 * Coordinate system: written in EPSG:4326 (lon, lat) by default.
 * Production-grade DXF for CAD consumers usually wants a projected
 * CRS (UTM zone matching the project) — out of scope for v1; flag as
 * a TODO when the spatial-reference picker lands.
 *
 * Review-status filter (CC-01 Item 6):
 *   - 'accepted' (default): silver.collars rows only
 *   - 'include_pending': silver.collars UNION review_queue.payload
 *     rows where target_table='silver.collars' AND lifecycle IN
 *     ('pending', 'in_review')
 *   - 'pending_only': only the review_queue.payload rows
 *
 * Returns array{path: string, size: int}.
 */
class DxfExporter
{
    public function export(string $projectId, array $filters = []): array
    {
        $reviewStatus = (string) ($filters['review_status'] ?? 'accepted');
        $collars = $this->fetchCollars($projectId, $filters, $reviewStatus);

        $tmpPath = sys_get_temp_dir().'/georag_collars_'.uniqid().'.dxf';
        $handle = fopen($tmpPath, 'w');
        if ($handle === false) {
            throw new \RuntimeException("Cannot open temp file for writing: {$tmpPath}");
        }

        try {
            fwrite($handle, $this->renderHeader());
            fwrite($handle, $this->renderTables());
            fwrite($handle, $this->renderEntitiesOpen());
            foreach ($collars as $row) {
                $east = (float) ($row->easting ?? $row->longitude ?? 0);
                $north = (float) ($row->northing ?? $row->latitude ?? 0);
                $elev = (float) ($row->elevation ?? 0);
                $holeId = (string) ($row->hole_id ?? '');
                fwrite($handle, $this->renderPoint($east, $north, $elev));
                if ($holeId !== '') {
                    fwrite($handle, $this->renderLabel($east, $north, $elev, $holeId));
                }
            }
            fwrite($handle, $this->renderFooter());
        } finally {
            fclose($handle);
        }

        return [
            'path' => $tmpPath,
            'size' => filesize($tmpPath),
        ];
    }

    /**
     * Fetch collar rows honouring the review-status filter.
     *
     * @return Collection<int, object>
     */
    private function fetchCollars(string $projectId, array $filters, string $reviewStatus): Collection
    {
        $silver = $reviewStatus === 'pending_only' ? collect() : $this->fetchSilverCollars($projectId, $filters);

        if ($reviewStatus === 'accepted') {
            return $silver;
        }

        // include_pending + pending_only need to add queued rows.
        $pending = $this->fetchPendingCollars($projectId, $filters);

        return $silver->concat($pending);
    }

    private function fetchSilverCollars(string $projectId, array $filters): Collection
    {
        $query = Collar::where('project_id', $projectId);
        if (! empty($filters['hole_type'])) {
            $query->where('hole_type', $filters['hole_type']);
        }
        if (! empty($filters['status'])) {
            $query->where('status', $filters['status']);
        }

        return $query->orderBy('hole_id')->get();
    }

    /**
     * @return Collection<int, object>
     */
    private function fetchPendingCollars(string $projectId, array $filters): Collection
    {
        // silver.review_queue.payload carries the same column shape as the
        // target silver row. For drill collars target_table is one of
        // 'silver.collars' or 'silver.drill_collars' depending on the
        // parser vintage — accept both for robustness.
        $rows = DB::table('silver.review_queue')
            ->where('project_id', $projectId)
            ->whereIn('target_table', ['silver.collars', 'silver.drill_collars'])
            ->whereIn('lifecycle', ['pending', 'in_review'])
            ->orderByDesc('updated_at')
            ->limit(5000)
            ->get(['payload']);

        return $rows->map(function ($row) {
            $payload = is_string($row->payload) ? json_decode($row->payload, true) : (array) $row->payload;
            $payload = is_array($payload) ? $payload : [];

            return (object) [
                'hole_id' => $payload['hole_id'] ?? '',
                'easting' => $payload['easting'] ?? $payload['longitude'] ?? null,
                'northing' => $payload['northing'] ?? $payload['latitude'] ?? null,
                'elevation' => $payload['elevation'] ?? 0,
            ];
        });
    }

    // ------------------------------------------------------------------
    // DXF text writers — AutoCAD 2018 (AC1027) ASCII
    // ------------------------------------------------------------------

    private function renderHeader(): string
    {
        // $ACADVER AC1027 = 2018; $INSUNITS 6 = metres.
        return implode("\n", [
            '  0', 'SECTION',
            '  2', 'HEADER',
            '  9', '$ACADVER',
            '  1', 'AC1027',
            '  9', '$INSUNITS',
            ' 70', '6',
            '  0', 'ENDSEC',
            '',
        ]);
    }

    private function renderTables(): string
    {
        // Minimal LAYER table with our two layers.
        return implode("\n", [
            '  0', 'SECTION',
            '  2', 'TABLES',
            '  0', 'TABLE',
            '  2', 'LAYER',
            ' 70', '2',
            '  0', 'LAYER',
            '  2', 'GEORAG_COLLARS',
            ' 70', '0',
            ' 62', '5',          // ACI 5 = blue
            '  6', 'CONTINUOUS',
            '  0', 'LAYER',
            '  2', 'GEORAG_COLLAR_LABELS',
            ' 70', '0',
            ' 62', '7',          // ACI 7 = white/black
            '  6', 'CONTINUOUS',
            '  0', 'ENDTAB',
            '  0', 'ENDSEC',
            '',
        ]);
    }

    private function renderEntitiesOpen(): string
    {
        return implode("\n", [
            '  0', 'SECTION',
            '  2', 'ENTITIES',
            '',
        ]);
    }

    private function renderPoint(float $x, float $y, float $z): string
    {
        return implode("\n", [
            '  0', 'POINT',
            '  8', 'GEORAG_COLLARS',
            ' 10', $this->fmt($x),
            ' 20', $this->fmt($y),
            ' 30', $this->fmt($z),
            '',
        ]);
    }

    private function renderLabel(float $x, float $y, float $z, string $text): string
    {
        // 5-metre east offset for the label so it doesn't overlap the point.
        return implode("\n", [
            '  0', 'TEXT',
            '  8', 'GEORAG_COLLAR_LABELS',
            ' 10', $this->fmt($x + 5.0),
            ' 20', $this->fmt($y),
            ' 30', $this->fmt($z),
            ' 40', '2.5',                // text height
            '  1', $this->escapeText($text),
            '',
        ]);
    }

    private function renderFooter(): string
    {
        return implode("\n", [
            '  0', 'ENDSEC',
            '  0', 'EOF',
            '',
        ]);
    }

    private function fmt(float $v): string
    {
        // DXF requires a literal decimal point; rtrim trailing zeros for clarity.
        return rtrim(rtrim(sprintf('%.6F', $v), '0'), '.') ?: '0';
    }

    private function escapeText(string $s): string
    {
        // DXF TEXT (group 1) doesn't support embedded newlines — collapse them.
        return str_replace(["\r", "\n"], ' ', $s);
    }
}
