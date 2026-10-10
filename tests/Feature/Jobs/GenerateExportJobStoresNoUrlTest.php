<?php

declare(strict_types=1);

namespace Tests\Feature\Jobs;

use App\Jobs\GenerateExportJob;
use App\Models\Collar;
use App\Models\Export;
use App\Models\Project;
use App\Services\StorageService;
use DateTimeInterface;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * An export on AWS failed after the file had been generated and uploaded.
 *
 * GenerateExportJob presigned a 24-hour URL and stored it in
 * silver.exports.download_url, a varchar(1000). Production signs with ECS
 * task-role session credentials, so the URL carries an X-Amz-Security-Token and
 * routinely runs past 1,000 characters: the UPDATE raised SQLSTATE 22001 and
 * the row ended 'failed'. The stored URL would also have died with the signing
 * session, hours before its recorded expiry.
 *
 * The job now records only the object key; ExportController mints a URL per
 * response (ExportControllerTest). Postgres-only because varchar lengths are not
 * enforced by SQLite, which is why nothing caught this before AWS.
 */
final class GenerateExportJobStoresNoUrlTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    /**
     * A project with one collar and two samples, and a pending csv_samples
     * export over it. csv_samples because it reads raw rows; the job's handling
     * of the finished file does not depend on which exporter produced it.
     *
     * @return array{0: Project, 1: Export}
     */
    private function pendingSamplesExport(): array
    {
        $project = Project::factory()->create();
        $collar = Collar::factory()->create(['project_id' => $project->project_id]);

        foreach ([[0.0, 1.0], [1.0, 2.0]] as [$from, $to]) {
            DB::table('silver.samples')->insert([
                'sample_id' => (string) Str::uuid(),
                'collar_id' => $collar->collar_id,
                'workspace_id' => $project->workspace_id,
                'from_depth' => $from,
                'to_depth' => $to,
                'sample_type' => 'core',
            ]);
        }

        $export = Export::create([
            'project_id' => $project->project_id,
            'workspace_id' => $project->workspace_id,
            'export_type' => 'csv_samples',
            'status' => 'pending',
            'filters' => [],
        ]);

        return [$project, $export];
    }

    public function test_a_completed_export_records_the_object_key_and_no_url(): void
    {
        Storage::fake('s3-exports');
        [, $export] = $this->pendingSamplesExport();

        // A signing double producing what ECS session credentials produce: a
        // URL no varchar(1000) could hold. The job must not need it.
        $storage = new class extends StorageService
        {
            public int $presignCalls = 0;

            public function presignedUrl(mixed $disk, string $key, ?DateTimeInterface $expiresAt = null): string
            {
                $this->presignCalls++;

                return 'https://georag-exports.s3.ca-central-1.amazonaws.com/'.$key
                    .'?X-Amz-Security-Token='.str_repeat('A', 2500);
            }
        };

        (new GenerateExportJob($export->export_id))->handle($storage);

        $export->refresh();
        $this->assertSame('completed', $export->status, (string) $export->error_message);
        $this->assertNull($export->error_message);
        $this->assertSame(1, $export->file_count);
        $this->assertGreaterThan(0, $export->total_size_bytes);
        $this->assertStringStartsWith($export->export_id.'/', (string) $export->minio_path);
        Storage::disk('s3-exports')->assertExists((string) $export->minio_path);

        $this->assertSame(0, $storage->presignCalls, 'the job must not mint a URL it has nowhere to keep');
        $this->assertNull($export->getRawOriginal('download_url'));
        $this->assertNull($export->getRawOriginal('download_url_expires_at'));
    }

    public function test_the_exported_file_contains_the_persisted_rows(): void
    {
        // The exporter ran for real: a header and one line per sample, not an
        // empty file that merely let the job finish.
        Storage::fake('s3-exports');
        [, $export] = $this->pendingSamplesExport();

        (new GenerateExportJob($export->export_id))->handle(app(StorageService::class));

        $export->refresh();
        $lines = array_filter(explode("\n", Storage::disk('s3-exports')->get((string) $export->minio_path)));
        $this->assertCount(3, $lines);
        $this->assertStringStartsWith('sample_id,collar_id,hole_id,', (string) reset($lines));
    }

    public function test_the_download_url_column_still_holds_a_session_token_sized_value(): void
    {
        // The safety net (2026_10_10_071834): a worker on the previous release
        // still stores the URL until it is replaced, and that must not fail on
        // the old width.
        $project = Project::factory()->create();
        $export = Export::create([
            'project_id' => $project->project_id,
            'workspace_id' => $project->workspace_id,
            'export_type' => 'csv_samples',
            'status' => 'completed',
            'filters' => [],
        ]);
        $url = 'https://georag-exports.s3.ca-central-1.amazonaws.com/k?X-Amz-Security-Token='.str_repeat('A', 2500);

        DB::table('silver.exports')->where('export_id', $export->export_id)->update(['download_url' => $url]);

        $this->assertSame(
            strlen($url),
            strlen((string) DB::table('silver.exports')->where('export_id', $export->export_id)->value('download_url')),
        );
        $this->assertSame('text', DB::selectOne(
            "SELECT data_type FROM information_schema.columns
              WHERE table_schema = 'silver' AND table_name = 'exports' AND column_name = 'download_url'",
        )->data_type);
    }
}
