<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use App\Models\VendorProfile;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\Storage;
use Tests\TestCase;

/**
 * Feature tests for the vendor_profile_id extension on UploadController::store.
 *
 * Coverage targets (5 tests):
 *   - Upload without vendor_profile_id succeeds (backward compat)
 *   - Upload with valid vendor_profile_id succeeds and echoes ID in response
 *   - Upload with non-existent vendor_profile_id returns 422
 *   - S3 put call includes Metadata when vendor_profile_id is provided
 *   - S3 put call has no Metadata key when vendor_profile_id is omitted
 *
 * A real Project row and project_user pivot entry are created in setUp() so
 * that UploadController::hasProjectAccess() returns true. This follows the
 * same pattern as ExportControllerTest.
 */
class UploadVendorProfileTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();

        // Create a real project and attach the user as owner so that the
        // UploadController's hasProjectAccess() check passes in SQLite.
        $this->project = Project::create([
            'project_name' => 'Upload Test Project '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);
    }

    // ─── Helpers ─────────────────────────────────────────────────────────────

    private function makeCsvFile(): UploadedFile
    {
        return UploadedFile::fake()->createWithContent(
            'collars.csv',
            "hole_id,easting,northing\nDH001,500000,6000000\n",
        );
    }

    private function uploadUrl(): string
    {
        return "/api/v1/projects/{$this->project->project_id}/upload";
    }

    // ─── Tests ───────────────────────────────────────────────────────────────

    public function test_upload_without_vendor_profile_id_succeeds(): void
    {
        Storage::fake('s3');

        $response = $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->makeCsvFile(),
                'category' => 'collars',
                // vendor_profile_id intentionally omitted
            ]);

        $response->assertCreated();

        // Response must NOT contain a vendor_profile_id key.
        $this->assertArrayNotHasKey(
            'vendor_profile_id',
            $response->json(),
            'vendor_profile_id should be absent from response when not provided',
        );

        // A file must have been stored.
        $files = Storage::disk('s3')->allFiles();
        $this->assertNotEmpty($files, 'Expected a file to be written to the S3 disk');
    }

    public function test_upload_with_valid_vendor_profile_id_succeeds_and_echoes_id(): void
    {
        Storage::fake('s3');

        $profile = VendorProfile::factory()->create();

        $response = $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->makeCsvFile(),
                'category' => 'collars',
                'vendor_profile_id' => $profile->id,
            ]);

        $response->assertCreated()
            ->assertJsonPath('vendor_profile_id', $profile->id);
    }

    public function test_upload_with_nonexistent_vendor_profile_id_returns_422(): void
    {
        Storage::fake('s3');

        $response = $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->makeCsvFile(),
                'category' => 'collars',
                'vendor_profile_id' => 999999, // does not exist in the DB
            ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['vendor_profile_id']);
    }

    public function test_s3_put_includes_metadata_when_vendor_profile_id_provided(): void
    {
        Storage::fake('s3');

        $profile = VendorProfile::factory()->create();

        // partialMock() lets us intercept specific calls while delegating all
        // other Storage facade calls (e.g. disk checks inside auth guards) to
        // the real implementation. The anonymous class proxy captures the put()
        // options and delegates the actual write to the fake disk.
        $capturedOptions = null;
        $fakeDisk = Storage::disk('s3');

        Storage::partialMock()
            ->shouldReceive('disk')
            ->with('s3')
            ->andReturnUsing(
                function () use ($fakeDisk, &$capturedOptions) {
                    return new class($fakeDisk, $capturedOptions)
                    {
                        public function __construct(
                            private readonly mixed $inner,
                            private mixed &$capturedOptions,
                        ) {}

                        public function put(string $path, mixed $contents, mixed $options = []): bool|string
                        {
                            $this->capturedOptions = $options;

                            if (is_resource($contents)) {
                                rewind($contents);
                                $this->inner->put($path, stream_get_contents($contents), $options);
                            } else {
                                $this->inner->put($path, $contents, $options);
                            }

                            return true;
                        }

                        public function __call(string $method, array $args): mixed
                        {
                            return $this->inner->{$method}(...$args);
                        }
                    };
                },
            );

        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->makeCsvFile(),
                'category' => 'collars',
                'vendor_profile_id' => $profile->id,
            ])
            ->assertCreated();

        $this->assertNotNull($capturedOptions, 'Storage::disk(s3)->put() was never called');
        $this->assertIsArray($capturedOptions);
        $this->assertArrayHasKey('Metadata', $capturedOptions);
        $this->assertSame(
            (string) $profile->id,
            $capturedOptions['Metadata']['x-georag-vendor-profile-id'],
            'x-georag-vendor-profile-id metadata must match the profile ID',
        );
    }

    public function test_s3_put_has_no_metadata_when_vendor_profile_id_omitted(): void
    {
        Storage::fake('s3');

        $capturedOptions = 'NOT_CALLED';
        $fakeDisk = Storage::disk('s3');

        Storage::partialMock()
            ->shouldReceive('disk')
            ->with('s3')
            ->andReturnUsing(
                function () use ($fakeDisk, &$capturedOptions) {
                    return new class($fakeDisk, $capturedOptions)
                    {
                        public function __construct(
                            private readonly mixed $inner,
                            private mixed &$capturedOptions,
                        ) {}

                        public function put(string $path, mixed $contents, mixed $options = []): bool|string
                        {
                            $this->capturedOptions = $options;

                            if (is_resource($contents)) {
                                rewind($contents);
                                $this->inner->put($path, stream_get_contents($contents), $options);
                            } else {
                                $this->inner->put($path, $contents, $options);
                            }

                            return true;
                        }

                        public function __call(string $method, array $args): mixed
                        {
                            return $this->inner->{$method}(...$args);
                        }
                    };
                },
            );

        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), [
                'file' => $this->makeCsvFile(),
                'category' => 'collars',
                // no vendor_profile_id
            ])
            ->assertCreated();

        $this->assertNotSame('NOT_CALLED', $capturedOptions, 'Storage::disk(s3)->put() was never called');
        $this->assertIsArray($capturedOptions);

        // When vendor_profile_id is absent the options must be empty so we
        // don't write a spurious x-georag-vendor-profile-id S3 header.
        $this->assertEmpty(
            $capturedOptions,
            'No Metadata should be set when vendor_profile_id is omitted',
        );
    }
}
