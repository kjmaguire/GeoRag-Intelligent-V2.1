<?php

declare(strict_types=1);

namespace App\Services\Exports;

use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

/**
 * CC-01 Item 6 — DXF exporter (CAD-compatible drillhole collar layer).
 *
 * Emits AutoCAD-2018 DXF (AC1027) ASCII directly. No ezdxf dependency
 * — the DXF format is a well-documented ASCII schema and a POINT-only
 * collar layer is a few hundred lines of templated text. Avoids
 * rebuilding the FastAPI container just for one exporter.
 *
 * Output shape:
 *   - A leading comment (group 999) naming the EPSG code and unit of x/y/z.
 *   - HEADER section with $ACADVER=AC1027 and $INSUNITS set from the CRS's own
 *     linear unit (6 metres, 2 feet, 21 US survey feet).
 *   - ENTITIES section with one POINT entity per collar, on layer
 *     "GEORAG_COLLARS". Each POINT carries a TEXT entity sibling with
 *     the hole_id annotation, offset 5 m east of the point.
 *
 * Coordinate system: ONE projected CRS for the whole drawing, because a DXF
 * has no per-entity CRS. It is the project's declared projected CRS, else the
 * UTM zone most of the exported collars sit in (the others are projected into
 * it too, with the distortion that implies far from its central meridian).
 * x/y come from silver.collars.geom_4326 transformed into it -- never from the
 * stored easting/northing, which carry whatever CRS and unit each upload used
 * (see CollarExportQuery). z is the collar elevation, metres converted to the
 * drawing unit. A DXF carries no CRS field, so the EPSG code is stated in the
 * leading comment; it was previously described here as EPSG:4326 (lon, lat)
 * while the code wrote the stored easting first, in an undeclared frame, under
 * a header that claimed metres.
 *
 * Review-status filter (CC-01 Item 6):
 *   - 'accepted' (default): silver.collars rows only
 *   - 'include_pending': silver.collars UNION review_queue.payload
 *     rows where target_table='silver.collars' AND lifecycle IN
 *     ('pending', 'in_review')
 *   - 'pending_only': only the review_queue.payload rows
 *   A queued row is placed from its payload's longitude/latitude (WGS84),
 *   projected like everything else. One with only easting/northing has no CRS
 *   to place it by and is left out, and the comment says how many were.
 *
 * Returns array{path: string, size: int}.
 */
class DxfExporter
{
    /** Label offset east of its point, and label height, in metres. */
    private const LABEL_OFFSET_M = 5.0;

    private const LABEL_HEIGHT_M = 2.5;

    /** DXF $INSUNITS codes for the linear units a projected CRS can have. */
    private const INSUNITS_METRES = 6;

    private const INSUNITS_FEET = 2;

    private const INSUNITS_US_SURVEY_FEET = 21;

    private const INSUNITS_UNITLESS = 0;

    /**
     * @param array<string, mixed> $filters
     *
     * @return array{path: string, size: int}
     */
    public function export(string $projectId, array $filters = []): array
    {
        $reviewStatus = (string) ($filters['review_status'] ?? 'accepted');
        $includeSilver = $reviewStatus !== 'pending_only';

        $pending = $reviewStatus === 'accepted' ? [] : $this->fetchPendingCollars($projectId);
        $placedPending = array_values(array_filter($pending, static fn (array $row): bool => $row['lon'] !== null));
        $omittedPending = count($pending) - count($placedPending);

        $epsg = $this->drawingEpsg($projectId, $filters, $includeSilver, $placedPending);
        $unit = $epsg !== null ? CollarExportQuery::linearUnit($epsg) : ['name' => 'metre', 'metres_per_unit' => 1.0];
        $metresPerUnit = $unit['metres_per_unit'] > 0 ? $unit['metres_per_unit'] : 1.0;

        $tmpPath = sys_get_temp_dir().'/georag_collars_'.uniqid().'.dxf';
        $handle = fopen($tmpPath, 'w');
        if ($handle === false) {
            throw new \RuntimeException("Cannot open temp file for writing: {$tmpPath}");
        }

        $unplaced = 0;

        try {
            fwrite($handle, $this->renderHeader($epsg, $unit['name'], $metresPerUnit, $omittedPending));
            fwrite($handle, $this->renderTables());
            fwrite($handle, $this->renderEntitiesOpen());

            if ($includeSilver) {
                $collars = CollarExportQuery::forProject($projectId, $filters, $epsg)
                    ->orderBy('hole_id')
                    ->orderBy('collar_id')
                    ->cursor();

                foreach ($collars as $collar) {
                    if ($collar->export_epsg === null) {
                        // No geom_4326: there is no position to draw.
                        $unplaced++;

                        continue;
                    }

                    $this->writeCollar(
                        $handle,
                        (float) $collar->export_easting,
                        (float) $collar->export_northing,
                        (float) ($collar->elevation ?? 0),
                        (string) $collar->hole_id,
                        $metresPerUnit,
                    );
                }
            }

            if ($placedPending !== [] && $epsg !== null) {
                $xy = CollarExportQuery::project(
                    array_map(static fn (array $row): array => [$row['lon'], $row['lat']], $placedPending),
                    $epsg,
                );

                foreach ($placedPending as $i => $row) {
                    $this->writeCollar($handle, $xy[$i][0], $xy[$i][1], $row['elevation'], $row['hole_id'], $metresPerUnit);
                }
            }

            fwrite($handle, $this->renderFooter());
        } finally {
            fclose($handle);
        }

        if ($unplaced > 0 || $omittedPending > 0) {
            Log::warning('dxf export: collars with no usable position were left out of the drawing', [
                'project_id' => $projectId,
                'without_geom_4326' => $unplaced,
                'pending_without_lon_lat' => $omittedPending,
            ]);
        }

        $size = filesize($tmpPath);
        if ($size === false) {
            throw new \RuntimeException("Cannot determine exported file size: {$tmpPath}");
        }

        return [
            'path' => $tmpPath,
            'size' => $size,
        ];
    }

    /**
     * The one CRS the drawing is written in: the project's projected CRS, else
     * the UTM zone most of its collars sit in (queued points count when they
     * are all there is). Null when nothing in the drawing has a position.
     *
     * @param array<string, mixed> $filters
     * @param list<array{hole_id: string, lon: ?float, lat: ?float, elevation: float}> $placedPending
     */
    private function drawingEpsg(string $projectId, array $filters, bool $includeSilver, array $placedPending): ?int
    {
        $epsg = CollarExportQuery::projectedEpsg($projectId);

        if ($epsg === null && $includeSilver) {
            $epsg = CollarExportQuery::modalUtmEpsg($projectId, $filters);
        }

        if ($epsg === null && $placedPending !== []) {
            $zones = array_count_values(array_map(
                static fn (array $row): int => CollarExportQuery::utmEpsg((float) $row['lon'], (float) $row['lat']),
                $placedPending,
            ));
            arsort($zones);
            $epsg = array_key_first($zones);
        }

        return $epsg;
    }

    /**
     * Queued (unreviewed) collar rows.
     *
     * silver.review_queue.payload carries the same column shape as the target
     * silver row. For drill collars target_table is one of 'silver.collars' or
     * 'silver.drill_collars' depending on the parser vintage -- accept both for
     * robustness.
     *
     * @return list<array{hole_id: string, lon: ?float, lat: ?float, elevation: float}>
     */
    private function fetchPendingCollars(string $projectId): array
    {
        $rows = DB::table('silver.review_queue')
            ->where('project_id', $projectId)
            ->whereIn('target_table', ['silver.collars', 'silver.drill_collars'])
            ->whereIn('lifecycle', ['pending', 'in_review'])
            ->orderByDesc('updated_at')
            ->limit(5000)
            ->get(['payload']);

        return $rows->map(static function (object $row): array {
            $payload = is_string($row->payload) ? json_decode($row->payload, true) : (array) $row->payload;
            $payload = is_array($payload) ? $payload : [];

            $located = is_numeric($payload['longitude'] ?? null) && is_numeric($payload['latitude'] ?? null);

            return [
                'hole_id' => (string) ($payload['hole_id'] ?? ''),
                'lon' => $located ? (float) $payload['longitude'] : null,
                'lat' => $located ? (float) $payload['latitude'] : null,
                'elevation' => is_numeric($payload['elevation'] ?? null) ? (float) $payload['elevation'] : 0.0,
            ];
        })->all();
    }

    /**
     * POINT, and its hole_id label when the collar has one.
     *
     * @param resource $handle
     */
    private function writeCollar($handle, float $east, float $north, float $elevationM, string $holeId, float $metresPerUnit): void
    {
        // The elevation column is metres whatever the drawing unit is.
        $z = $elevationM / $metresPerUnit;

        fwrite($handle, $this->renderPoint($east, $north, $z));
        if ($holeId !== '') {
            fwrite($handle, $this->renderLabel($east, $north, $z, $holeId, $metresPerUnit));
        }
    }

    // ------------------------------------------------------------------
    // DXF text writers — AutoCAD 2018 (AC1027) ASCII
    // ------------------------------------------------------------------

    private function renderHeader(?int $epsg, string $unitName, float $metresPerUnit, int $omittedPending): string
    {
        // Group 999 is a comment. It is the only place a DXF can say which CRS
        // its numbers are in; ezdxf and AutoCAD both skip it.
        $lines = [
            '999',
            $epsg !== null
                ? "GeoRAG collar export. x/y are EPSG:{$epsg} ({$unitName}); z is the collar elevation in the same unit."
                : 'GeoRAG collar export. No collar in this drawing has a position.',
        ];
        if ($omittedPending > 0) {
            $lines[] = '999';
            $lines[] = "{$omittedPending} queued collar(s) were left out: no longitude/latitude to place them by.";
        }

        // $ACADVER AC1027 = 2018; $INSUNITS from the CRS's own linear unit.
        return implode("\n", array_merge($lines, [
            '  0', 'SECTION',
            '  2', 'HEADER',
            '  9', '$ACADVER',
            '  1', 'AC1027',
            '  9', '$INSUNITS',
            ' 70', (string) $this->insUnits($metresPerUnit),
            '  0', 'ENDSEC',
            '',
        ]));
    }

    /**
     * $INSUNITS for a linear unit given as metres per unit. 0 (unitless) for a
     * unit with no code of its own, rather than a wrong one.
     */
    private function insUnits(float $metresPerUnit): int
    {
        return match (true) {
            abs($metresPerUnit - 1.0) < 1e-9 => self::INSUNITS_METRES,
            abs($metresPerUnit - 0.3048) < 1e-9 => self::INSUNITS_FEET,
            abs($metresPerUnit - 0.3048006096012192) < 1e-9 => self::INSUNITS_US_SURVEY_FEET,
            default => self::INSUNITS_UNITLESS,
        };
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

    private function renderLabel(float $x, float $y, float $z, string $text, float $metresPerUnit): string
    {
        // 5 m east of the point so the label does not overlap it, 2.5 m high --
        // in drawing units, which are not metres in a foot-based CRS.
        return implode("\n", [
            '  0', 'TEXT',
            '  8', 'GEORAG_COLLAR_LABELS',
            ' 10', $this->fmt($x + self::LABEL_OFFSET_M / $metresPerUnit),
            ' 20', $this->fmt($y),
            ' 30', $this->fmt($z),
            ' 40', $this->fmt(self::LABEL_HEIGHT_M / $metresPerUnit),
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
