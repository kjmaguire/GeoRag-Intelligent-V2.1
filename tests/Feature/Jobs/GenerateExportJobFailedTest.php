<?php

declare(strict_types=1);

namespace Tests\Feature\Jobs;

use App\Jobs\GenerateExportJob;
use App\Models\Export;
use App\Models\Project;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Queue\MaxAttemptsExceededException;
use Illuminate\Queue\TimeoutExceededException;
use RuntimeException;
use Tests\TestCase;

/**
 * LAR-6 (2026-09-29 audit): a worker-side death must not leave an export
 * `running` forever.
 *
 * A Horizon timeout (SIGALRM) or a MaxAttemptsExceeded re-pop never reaches
 * handle()'s catch; only failed() runs. Without it, status polling never
 * terminated and download answered 409 indefinitely.
 */
final class GenerateExportJobFailedTest extends TestCase
{
    use RefreshDatabase;

    private function export(string $status, ?string $errorMessage = null): Export
    {
        $project = Project::create([
            'project_name' => 'Export Failed Hook '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        return Export::create([
            'project_id' => $project->project_id,
            'export_type' => 'geopackage',
            'status' => $status,
            'error_message' => $errorMessage,
        ]);
    }

    public function test_a_timed_out_running_export_is_marked_failed_with_a_reason(): void
    {
        $export = $this->export('running');

        (new GenerateExportJob($export->export_id))->failed(new TimeoutExceededException('GenerateExportJob has timed out.'));

        $export->refresh();
        $this->assertSame('failed', $export->status);
        $this->assertStringContainsString('timed out after 300 seconds', (string) $export->error_message);
    }

    public function test_an_interrupted_pending_export_is_marked_failed(): void
    {
        $export = $this->export('pending');

        (new GenerateExportJob($export->export_id))->failed(new MaxAttemptsExceededException('attempted too many times'));

        $export->refresh();
        $this->assertSame('failed', $export->status);
        $this->assertStringContainsString('interrupted', (string) $export->error_message);
    }

    public function test_an_unexpected_exception_gets_a_generic_reason_not_its_message(): void
    {
        $export = $this->export('running');

        (new GenerateExportJob($export->export_id))->failed(
            new RuntimeException('SQLSTATE[42P01]: relation "silver.secret_table" does not exist'),
        );

        $export->refresh();
        $this->assertSame('failed', $export->status);
        $this->assertStringNotContainsString('SQLSTATE', (string) $export->error_message);
    }

    public function test_the_specific_message_written_by_handle_is_not_overwritten(): void
    {
        $export = $this->export('failed', 'No collars match the filter.');

        (new GenerateExportJob($export->export_id))->failed(new RuntimeException('No collars match the filter.'));

        $this->assertSame('No collars match the filter.', $export->refresh()->error_message);
    }

    public function test_a_completed_export_is_never_touched(): void
    {
        $export = $this->export('completed');

        (new GenerateExportJob($export->export_id))->failed(new TimeoutExceededException('late'));

        $export->refresh();
        $this->assertSame('completed', $export->status);
        $this->assertNull($export->error_message);
    }

    public function test_a_missing_export_row_is_a_no_op(): void
    {
        (new GenerateExportJob('00000000-0000-0000-0000-000000000000'))->failed(null);

        $this->assertSame(0, Export::query()->count());
    }
}
