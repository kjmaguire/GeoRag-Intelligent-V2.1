<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\Request as ClientRequest;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * A ZIP must be able to declare the coordinate system of what is inside it.
 *
 * THE BUG THIS PINS
 *     `ingest_tabular` resolves `epsg = input.source_epsg or
 *     DEFAULT_SOURCE_EPSG` (32613, the Athabasca Basin) and never consults
 *     the project. `dispatchZipExtraction()` did not accept a $sourceEpsg at
 *     all, so every collar and surface sample inside an archive was written
 *     as EPSG:32613 wherever it actually came from.
 *
 *     Measured on RedStar's Sitka collars (EPSG:26904, easting 400807,
 *     northing 6117291): written as 32613 they land at
 *     POINT(-106.5582 55.1922) — northern Saskatchewan, 3,430 km east of
 *     Unga Island. The map, the exports and the agent all agree on the
 *     wrong place.
 *
 *     There was no escape hatch either. `supportsCrsOverride()` excluded the
 *     `archive` category, so the wizard rendered no CRS field for a ZIP —
 *     while the run's warning told the user to "re-upload with the correct
 *     EPSG code", advice that cannot be followed for a file inside an
 *     archive without unzipping the delivery and uploading file by file.
 *
 * WHY THE ASSERTIONS ARE ON THE DISPATCHED PAYLOAD
 *     The whole failure is a value being collected and then dropped between
 *     layers. A 201 proves nothing — the upload always succeeded. What has
 *     to be pinned is that the operator's EPSG actually reaches the workflow
 *     trigger.
 */
class UploadZipSourceEpsgTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    private Project $project;

    /** @var list<array{url: string, data: array<string, mixed>}> */
    private array $captured = [];

    protected function setUp(): void
    {
        parent::setUp();

        $this->project = Project::create([
            'project_name' => 'Zip CRS '.uniqid(),
            'crs_datum' => 'EPSG:26904',
            'orientation_reference' => 'BOH',
        ]);
        DB::table('silver.projects')
            ->where('project_id', $this->project->project_id)
            ->update(['workspace_id' => (string) Str::uuid()]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);

        config(['services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long']);

        Storage::fake('s3');

        $this->captured = [];
        Http::fake(function (ClientRequest $request) {
            $this->captured[] = ['url' => $request->url(), 'data' => $request->data()];

            return Http::response([
                'workflow_run_id' => 'test-workflow-run-id',
                'hatchet_workflow_run_id' => 'test-hatchet-run-id',
            ], 202);
        });
    }

    private function uploadUrl(): string
    {
        return "/api/v1/projects/{$this->project->project_id}/upload";
    }

    /** A structurally valid empty ZIP (end-of-central-directory only). */
    private function zip(string $name = 'delivery.zip'): UploadedFile
    {
        return UploadedFile::fake()->createWithContent(
            $name,
            "PK\x05\x06".str_repeat("\x00", 18),
        );
    }

    /** @return array<string, mixed>|null */
    private function zipTriggerPayload(): ?array
    {
        foreach ($this->captured as $call) {
            if (str_contains($call['url'], '/shadow/ingest_zip_archive/trigger')) {
                return $call['data'];
            }
        }

        return null;
    }

    public function test_the_operators_epsg_reaches_the_zip_workflow(): void
    {
        // Sitka's real CRS. Deliberately not 32613 — an assertion written at
        // the Athabasca default would pass against the bug.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->zip(),
                'category' => 'archive',
                'source_epsg' => 26904,
            ])
            ->assertCreated();

        $payload = $this->zipTriggerPayload();

        $this->assertNotNull($payload, 'ingest_zip_archive was never triggered');
        $this->assertSame(
            26904,
            $payload['source_epsg'] ?? null,
            'the EPSG the operator typed must reach the workflow; without it '
                .'every member is written as the Athabasca default, EPSG:32613',
        );
    }

    public function test_the_key_is_omitted_when_no_epsg_was_declared(): void
    {
        // Absence and null are different facts here. IngestZipArchiveInput
        // defaults the field, and the absence is what tells ingest_tabular
        // to fall back — a literal null would be indistinguishable from a
        // declared one if the field ever stops being Optional.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->zip(),
                'category' => 'archive',
            ])
            ->assertCreated();

        $payload = $this->zipTriggerPayload();

        $this->assertNotNull($payload);
        $this->assertArrayNotHasKey('source_epsg', $payload);
    }

    public function test_an_out_of_range_epsg_is_refused_at_the_door(): void
    {
        // Same 1024..32767 window the workflow input and the
        // crs_epsg_native CHECK use. Refusing here beats failing at persist,
        // by which time the uploader is long gone.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->zip(),
                'category' => 'archive',
                'source_epsg' => 999,
            ])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['source_epsg']);
    }

    public function test_the_declared_epsg_is_echoed_back_to_the_caller(): void
    {
        // So the wizard can show what was understood rather than what was
        // typed — the two diverging silently is the whole bug.
        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->zip(),
                'category' => 'archive',
                'source_epsg' => 26904,
            ])
            ->assertCreated()
            ->assertJsonPath('source_epsg', 26904);
    }
}
