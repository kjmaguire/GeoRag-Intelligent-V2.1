<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * When the hand-off to FastAPI throws, store() answers 502 with the dispatch
 * outcome in the body. That body goes to the browser, so it must not carry the
 * exception text (an internal FastAPI URL, a connection string).
 */
class UploadDispatchErrorDisclosureTest extends TestCase
{
    use RefreshDatabase;

    private const INTERNAL = 'cURL error 7: Failed to connect to fastapi.georag.internal port 8000';

    public function test_a_dispatch_exception_is_neutral_in_the_502_body_and_detailed_in_the_log(): void
    {
        $project = Project::create([
            'project_name' => 'Dispatch Disclosure '.uniqid(),
            'crs_datum' => 'EPSG:26904',
            'orientation_reference' => 'BOH',
        ]);
        DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->update(['workspace_id' => (string) Str::uuid()]);
        $user = User::factory()->create();
        $user->projects()->attach($project->project_id, ['role' => 'owner']);

        config(['services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long']);
        Storage::fake('s3');
        Http::fake(fn () => throw new ConnectionException(self::INTERNAL));
        Log::spy();

        $response = $this->actingAs($user)->postJson(
            "/api/v1/projects/{$project->project_id}/upload",
            [
                'file' => UploadedFile::fake()->createWithContent('scan.png', "\x89PNG\r\n\x1a\n".str_repeat("\x00", 64)),
                'category' => 'reports',
            ],
        );

        $response->assertStatus(502);
        $this->assertSame('dispatch_exception', $response->json('ingest.reason'));
        $this->assertStringNotContainsString('georag.internal', (string) $response->getContent());

        Log::shouldHaveReceived('warning')->withArgs(
            fn (string $message, array $context = []): bool => str_contains((string) ($context['error'] ?? ''), 'georag.internal'),
        )->atLeast()->once();
    }
}
