<?php

declare(strict_types=1);

namespace Tests\Feature\Jobs;

use App\Jobs\GenerateExportJob;
use App\Models\Export;
use App\Models\Project;
use App\Services\StorageService;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Log;
use Tests\TestCase;

/**
 * exports.error_message is returned to the browser with the model, so a raw
 * exception message (SQL, a path, an internal URL) must never be stored there.
 */
final class GenerateExportJobNeutralErrorTest extends TestCase
{
    use RefreshDatabase;

    public function test_a_generation_exception_stores_a_neutral_message_and_logs_the_real_one(): void
    {
        Log::spy();
        $project = Project::create([
            'project_name' => 'Neutral error '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);
        // An unknown type throws from generate() carrying the supplied string.
        $export = Export::create([
            'project_id' => $project->project_id,
            'export_type' => 'http://internal.georag.local:9000/secret-bucket',
            'status' => 'pending',
        ]);

        try {
            (new GenerateExportJob($export->export_id))->handle($this->createMock(StorageService::class));
            $this->fail('handle() must rethrow so the queue records the failure');
        } catch (\InvalidArgumentException) {
            // expected
        }

        $export->refresh();
        $this->assertSame('failed', $export->status);
        $this->assertSame(GenerateExportJob::GENERIC_FAILURE_MESSAGE, $export->error_message);
        $this->assertStringNotContainsString('internal.georag.local', (string) $export->error_message);

        Log::shouldHaveReceived('error')->withArgs(
            fn (string $message, array $context): bool => $message === 'GenerateExportJob: failed'
                && str_contains((string) $context['exception'], 'internal.georag.local'),
        )->once();
    }
}
