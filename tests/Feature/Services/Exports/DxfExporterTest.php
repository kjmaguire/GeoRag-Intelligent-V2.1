<?php

declare(strict_types=1);

namespace Tests\Feature\Services\Exports;

use App\Models\Project;
use App\Services\Exports\DxfExporter;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\Concerns\SeedsCollarExportData;
use Tests\TestCase;

/**
 * dxf, run on persisted rows.
 *
 * The exporter's docblock said it wrote EPSG:4326 (lon, lat). It wrote the
 * stored easting / northing instead, in whatever frame each upload used, under
 * a header declaring $INSUNITS 6 (metres) even when the numbers were US feet or
 * degrees. A DXF has no CRS field, so the CRS is now one projected system for
 * the whole drawing, stated in a leading comment, with $INSUNITS taken from its
 * own linear unit.
 *
 * Postgres-only: the coordinates go through PostGIS.
 */
final class DxfExporterTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;
    use SeedsCollarExportData;

    /**
     * Group code => value pairs of the first POINT entity.
     *
     * @return list<array{x: float, y: float, z: float}> every POINT in the drawing
     */
    private function points(string $dxf): array
    {
        $lines = array_map('trim', explode("\n", $dxf));
        $points = [];
        for ($i = 0; $i < count($lines) - 1; $i += 2) {
            if ($lines[$i] === '0' && $lines[$i + 1] === 'POINT') {
                $point = [];
                for ($j = $i + 2; $j < count($lines) - 1 && $lines[$j] !== '0'; $j += 2) {
                    $point[$lines[$j]] = $lines[$j + 1];
                }
                $points[] = ['x' => (float) $point['10'], 'y' => (float) $point['20'], 'z' => (float) $point['30']];
            }
        }

        return $points;
    }

    private function insUnits(string $dxf): int
    {
        $lines = array_map('trim', explode("\n", $dxf));
        $at = array_search('$INSUNITS', $lines, true);
        $this->assertNotFalse($at, 'the header declares $INSUNITS');

        return (int) $lines[$at + 2];
    }

    private function exportDxf(string $projectId, array $filters = []): string
    {
        $path = (new DxfExporter)->export($projectId, $filters)['path'];
        $dxf = (string) file_get_contents($path);
        @unlink($path);

        return $dxf;
    }

    public function test_points_are_the_geometry_in_the_projects_crs_not_the_stored_columns(): void
    {
        $project = $this->exportProject(26913);
        $this->exportCollar($project, 'DXF-1', ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913], ['elevation' => 412.5]);

        $dxf = $this->exportDxf($project->project_id);
        $points = $this->points($dxf);

        $this->assertCount(1, $points);
        $this->assertEqualsWithDelta(500000.0, $points[0]['x'], 0.01);
        $this->assertEqualsWithDelta(6000000.0, $points[0]['y'], 0.01);
        $this->assertEqualsWithDelta(412.5, $points[0]['z'], 0.001);
        $this->assertSame(6, $this->insUnits($dxf), 'metres');
        $this->assertStringContainsString('EPSG:26913 (metre)', $dxf);
    }

    public function test_a_foot_based_crs_is_declared_in_feet_and_the_labels_scale_with_it(): void
    {
        // EPSG:2263 is in US survey feet. The numbers are feet, so $INSUNITS
        // must say feet (21), and "5 m east" / "2.5 m high" must be written as
        // 5 m / 0.3048006... in drawing units, not as 5 and 2.5 feet.
        $project = $this->exportProject(2263);
        $this->exportCollar($project, 'DXF-2', ['x' => 1000000.0, 'y' => 200000.0, 'srid' => 2263], ['elevation' => 100.0]);

        $dxf = $this->exportDxf($project->project_id);
        $points = $this->points($dxf);

        $this->assertSame(21, $this->insUnits($dxf));
        $this->assertStringContainsString('EPSG:2263 (US survey foot)', $dxf);
        $this->assertEqualsWithDelta(1000000.0, $points[0]['x'], 0.01);
        $this->assertEqualsWithDelta(200000.0, $points[0]['y'], 0.01);
        // 100 m of elevation in feet.
        $this->assertEqualsWithDelta(100.0 / 0.3048006096012192, $points[0]['z'], 0.001);

        $this->assertMatchesRegularExpression('/\n 10\n1000016\.4\d*\n/', $dxf, 'label sits 5 m east of the point');
        $this->assertMatchesRegularExpression('/\n 40\n8\.202\d*\n/', $dxf, 'label is 2.5 m high');
    }

    public function test_a_project_with_no_crs_uses_one_utm_zone_for_the_whole_drawing(): void
    {
        // Three collars in zone 13 and one in zone 12: a drawing has one CRS,
        // so the minority collar is projected INTO zone 13, not left in its own.
        $project = $this->exportProject();
        foreach (['A', 'B', 'C'] as $hole) {
            $this->exportCollar($project, "Z13-{$hole}", ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 32613]);
        }
        $this->exportCollar($project, 'Z12-A', ['x' => 450000.0, 'y' => 6000000.0, 'srid' => 32612]);

        $dxf = $this->exportDxf($project->project_id);
        $points = $this->points($dxf);

        $this->assertStringContainsString('EPSG:32613 (metre)', $dxf);
        $this->assertCount(4, $points);

        // The zone-12 collar, expressed in zone 13, lies west of 500000.
        $this->assertCount(1, array_filter($points, fn (array $p): bool => $p['x'] < 450000.0));
        // ...and the zone-13 ones are exactly where they were.
        $this->assertCount(3, array_filter($points, fn (array $p): bool => abs($p['x'] - 500000.0) < 0.01));
    }

    public function test_the_hole_type_and_status_filters_ignore_case(): void
    {
        $project = $this->exportProject(26913);
        $at = ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913];
        $this->exportCollar($project, 'KEEP', $at, ['hole_type' => 'Diamond', 'status' => 'active']);
        $this->exportCollar($project, 'DROP', $at, ['hole_type' => 'RC', 'status' => 'active']);

        $dxf = $this->exportDxf($project->project_id, ['hole_type' => 'DIAMOND', 'status' => 'Active']);

        $this->assertStringContainsString("\nKEEP\n", $dxf);
        $this->assertStringNotContainsString("\nDROP\n", $dxf);
    }

    public function test_a_collar_with_no_geometry_is_left_out_rather_than_drawn_in_an_unknown_frame(): void
    {
        $project = $this->exportProject(26913);
        $this->exportCollar($project, 'PLACED', ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913]);
        $this->exportCollar($project, 'UNPLACED', null);

        $dxf = $this->exportDxf($project->project_id);

        $this->assertCount(1, $this->points($dxf));
        $this->assertStringNotContainsString('UNPLACED', $dxf);
    }

    public function test_an_empty_project_still_yields_a_valid_drawing(): void
    {
        $project = $this->exportProject();

        $dxf = $this->exportDxf($project->project_id);

        $this->assertSame([], $this->points($dxf));
        $this->assertStringContainsString('No collar in this drawing has a position', $dxf);
        $this->assertStringEndsWith("  0\nEOF\n", $dxf);
    }

    public function test_queued_collars_are_placed_by_their_longitude_and_latitude(): void
    {
        // include_pending: a queued row that carries lon/lat is projected into
        // the drawing's CRS; one with only easting/northing has no CRS to be
        // placed by and is left out, with the drawing saying so.
        $project = $this->exportProject(26913);
        $this->queueCollar($project, ['hole_id' => 'Q-LONLAT', 'longitude' => -105.0, 'latitude' => 54.0, 'elevation' => 300]);
        $this->queueCollar($project, ['hole_id' => 'Q-FRAMELESS', 'easting' => 1650000, 'northing' => 250000]);

        $dxf = $this->exportDxf($project->project_id, ['review_status' => 'pending_only']);
        $points = $this->points($dxf);

        $this->assertCount(1, $points);
        // -105 is the central meridian of zone 13: easting 500000.
        $this->assertEqualsWithDelta(500000.0, $points[0]['x'], 5.0);
        $this->assertStringContainsString("\nQ-LONLAT\n", $dxf);
        $this->assertStringNotContainsString('Q-FRAMELESS', $dxf);
        $this->assertStringContainsString('1 queued collar(s) were left out', $dxf);
    }

    public function test_pending_only_does_not_include_the_accepted_collars(): void
    {
        $project = $this->exportProject(26913);
        $this->exportCollar($project, 'ACCEPTED', ['x' => 500000.0, 'y' => 6000000.0, 'srid' => 26913]);
        $this->queueCollar($project, ['hole_id' => 'QUEUED', 'longitude' => -105.0, 'latitude' => 54.0]);

        $pendingOnly = $this->exportDxf($project->project_id, ['review_status' => 'pending_only']);
        $both = $this->exportDxf($project->project_id, ['review_status' => 'include_pending']);

        $this->assertStringNotContainsString('ACCEPTED', $pendingOnly);
        $this->assertStringContainsString('QUEUED', $pendingOnly);
        $this->assertStringContainsString('ACCEPTED', $both);
        $this->assertStringContainsString('QUEUED', $both);
    }

    /**
     * @param array<string, mixed> $payload
     */
    private function queueCollar(Project $project, array $payload): void
    {
        DB::table('silver.review_queue')->insert([
            'queue_id' => (string) Str::uuid(),
            'workspace_id' => $project->workspace_id,
            'project_id' => $project->project_id,
            'target_table' => 'silver.collars',
            'target_record_kind' => 'collar',
            'bronze_uri' => 'bronze://test/'.Str::uuid(),
            'payload' => json_encode($payload),
            'confidence_record' => 0.5,
            'parser_version' => 'test',
            'routing_decision' => 'review_required',
            'lifecycle' => 'pending',
        ]);
    }
}
