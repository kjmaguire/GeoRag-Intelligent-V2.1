<?php

namespace Tests\Feature\Api\V1;

use App\Enums\HoleType;
use App\Http\Requests\StoreExportRequest;
use App\Jobs\GenerateExportJob;
use App\Models\Export;
use App\Models\Project;
use App\Models\User;
use Carbon\Carbon;
use Closure;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Queue;
use Illuminate\Support\Facades\Storage;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * Feature tests for ExportController.
 *
 * Runs under the suite's sqlite-in-memory fixture (see tests/bootstrap.php and
 * TestCase::refreshApplication for the PG→sqlite DDL rewrites). RefreshDatabase
 * rebuilds the schema per test, matching every other V1 controller test.
 *
 * Queue::fake() is used throughout so that GenerateExportJob is never actually
 * dispatched — we assert that it is queued with the correct export_id, not that
 * the file generation itself works (that is covered by service-level unit tests).
 */
class ExportControllerTest extends TestCase
{
    use RefreshDatabase;

    /** The bucket-scoped key GenerateExportJob records in `minio_path`. */
    private const OBJECT_KEY = '0d4f9c2e-7b1a-4c53-9a58-3f6e1b2d8c90/georag_collars_65a1f.csv';

    private Project $project;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        // StorageService::exports() resolves this disk; faked, its
        // temporaryUrl() is deterministic and needs no credentials.
        Storage::fake('s3-exports');

        $this->project = Project::create([
            'project_name' => 'Export Test Project '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);
        $this->actingAs($this->user);
    }

    // -------------------------------------------------------------------------
    // store — CSV collars
    // -------------------------------------------------------------------------

    public function test_can_create_csv_collars_export(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => 'csv_collars'],
        );

        $response->assertStatus(202)
            ->assertJsonPath('data.export_type', 'csv_collars')
            ->assertJsonPath('data.status', 'pending')
            ->assertJsonPath('data.project_id', $this->project->project_id)
            ->assertJsonStructure([
                'data' => ['export_id', 'export_type', 'status', 'project_id', 'created_at'],
                'status_url',
                'message',
            ]);

        $exportId = $response->json('data.export_id');
        $this->assertNotEmpty($exportId);

        Queue::assertPushed(GenerateExportJob::class, function (GenerateExportJob $job) use ($exportId) {
            // Verify the job carries the right export_id by reflecting the property.
            $reflection = new \ReflectionProperty($job, 'exportId');
            $reflection->setAccessible(true);

            return $reflection->getValue($job) === $exportId;
        });
    }

    // -------------------------------------------------------------------------
    // store — CSA bundle
    // -------------------------------------------------------------------------

    public function test_can_create_csa_bundle(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => 'csa_bundle'],
        );

        $response->assertStatus(202)
            ->assertJsonPath('data.export_type', 'csa_bundle')
            ->assertJsonPath('data.status', 'pending');

        Queue::assertPushed(GenerateExportJob::class);
    }

    // -------------------------------------------------------------------------
    // store — with filters
    // -------------------------------------------------------------------------

    public function test_can_create_export_with_filters(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            [
                'export_type' => 'csv_collars',
                'filters' => [
                    'hole_type' => 'Diamond',
                    'min_depth' => 100.0,
                    'max_depth' => 500.0,
                ],
            ],
        );

        $response->assertStatus(202);

        $export = Export::find($response->json('data.export_id'));
        $this->assertNotNull($export);
        $this->assertSame('Diamond', $export->filters['hole_type']);
        $this->assertEquals(100.0, $export->filters['min_depth']);
    }

    // -------------------------------------------------------------------------
    // store — collar vocabularies read off the enums
    // -------------------------------------------------------------------------

    /**
     * hole_type / status filters were two hand-copied `in:` lists that had
     * already drifted from HoleType / CollarStatus, and compared case-sensitively
     * while the ingestion writes 'active'. They are read off the enums now and
     * accept any case.
     *
     * @return iterable<string, array{string, string}>
     */
    public static function acceptedCollarFilters(): iterable
    {
        // Every value the old lists accepted still is.
        foreach (['Diamond', 'RC', 'RAB', 'Rotary', 'Percussion'] as $value) {
            yield "hole_type {$value}" => ['hole_type', $value];
        }
        foreach (['Active', 'Completed', 'Abandoned'] as $value) {
            yield "status {$value}" => ['status', $value];
        }

        // Cases the enums have and the old lists did not.
        yield 'hole_type Auger' => ['hole_type', 'Auger'];
        yield 'hole_type exploration' => ['hole_type', 'exploration'];
        yield 'hole_type unknown' => ['hole_type', 'unknown'];
        yield 'status In Progress' => ['status', 'In Progress'];
        yield 'status Planned' => ['status', 'Planned'];
        yield 'status active' => ['status', 'active'];

        // Any letter case.
        yield 'hole_type diamond' => ['hole_type', 'diamond'];
        yield 'hole_type RC lowercase' => ['hole_type', 'rc'];
        yield 'status COMPLETED' => ['status', 'COMPLETED'];
    }

    #[DataProvider('acceptedCollarFilters')]
    public function test_collar_filters_accept_every_enum_value_in_any_case(string $filter, string $value): void
    {
        Queue::fake();

        $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => 'csv_collars', 'filters' => [$filter => $value]],
        )->assertStatus(202);
    }

    /**
     * @return iterable<string, array{string, string}>
     */
    public static function rejectedCollarFilters(): iterable
    {
        yield 'hole_type not a method' => ['hole_type', 'Granite'];
        yield 'hole_type typo' => ['hole_type', 'Diamnd'];
        yield 'status a free word' => ['status', 'Closed'];
    }

    #[DataProvider('rejectedCollarFilters')]
    public function test_collar_filters_reject_what_is_not_in_the_vocabulary(string $filter, string $value): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => 'csv_collars', 'filters' => [$filter => $value]],
        );

        $response->assertUnprocessable()->assertJsonValidationErrors(["filters.{$filter}"]);
        Queue::assertNothingPushed();
    }

    public function test_the_filter_rules_follow_the_enums_rather_than_a_copy_of_them(): void
    {
        // A case added to an enum is accepted with no edit to the request.
        $rules = (new StoreExportRequest)->rules();
        $closure = collect($rules['filters.hole_type'])->first(fn ($rule) => $rule instanceof Closure);
        $this->assertInstanceOf(Closure::class, $closure);

        foreach (HoleType::cases() as $case) {
            $failed = false;
            $closure('filters.hole_type', $case->value, function () use (&$failed): void {
                $failed = true;
            });
            $this->assertFalse($failed, "HoleType::{$case->name} must be accepted");
        }
    }

    // -------------------------------------------------------------------------
    // store — validation failures
    // -------------------------------------------------------------------------

    public function test_store_returns_422_for_invalid_export_type(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            ['export_type' => 'invalid_type'],
        );

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['export_type']);

        Queue::assertNothingPushed();
    }

    public function test_store_returns_422_when_export_type_missing(): void
    {
        Queue::fake();

        $response = $this->postJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
            [],
        );

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['export_type']);
    }

    public function test_store_returns_403_for_non_member_project(): void
    {
        Queue::fake();

        // Non-member requests MUST get 403, not 404 — that way the API cannot
        // be used to enumerate which project UUIDs exist.
        $response = $this->postJson(
            '/api/v1/projects/00000000-0000-0000-0000-000000000000/exports',
            ['export_type' => 'csv_collars'],
        );

        $response->assertForbidden();
        Queue::assertNothingPushed();
    }

    // -------------------------------------------------------------------------
    // show — status polling
    // -------------------------------------------------------------------------

    public function test_can_fetch_export_status(): void
    {
        $export = Export::create([
            'project_id' => $this->project->project_id,
            'export_type' => 'csv_collars',
            'status' => 'pending',
            'filters' => [],
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports/{$export->export_id}",
        );

        $response->assertOk()
            ->assertJsonPath('data.export_id', $export->export_id)
            ->assertJsonPath('data.status', 'pending')
            ->assertJsonPath('data.export_type', 'csv_collars');
    }

    public function test_show_returns_download_url_when_completed(): void
    {
        $export = $this->completedExport();

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports/{$export->export_id}",
        );

        $response->assertOk()
            ->assertJsonPath('data.status', 'completed');

        // Minted from the stored object key for THIS response, not read back
        // from the row.
        $url = $response->json('data.download_url');
        $this->assertIsString($url);
        $this->assertStringContainsString(self::OBJECT_KEY, $url);
    }

    public function test_show_mints_a_short_lived_url_and_reports_when_it_expires(): void
    {
        $export = $this->completedExport();

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports/{$export->export_id}",
        );

        // Storage::fake() puts the expiry in the URL's own query string, so the
        // two can be checked against each other and against the clock.
        parse_str((string) parse_url($response->json('data.download_url'), PHP_URL_QUERY), $query);
        $signedUntil = (int) $query['expiration'];

        $this->assertEqualsWithDelta(now()->addMinutes(5)->getTimestamp(), $signedUntil, 5);
        $this->assertEqualsWithDelta(
            $signedUntil,
            Carbon::parse($response->json('data.download_url_expires_at'))->getTimestamp(),
            1,
        );
    }

    public function test_show_ignores_a_url_stored_by_the_previous_release(): void
    {
        // The old job stored a 24-hour URL and the controller served it until
        // the stored expiry. With ECS task-role credentials that URL died when
        // the signing session did, hours before download_url_expires_at.
        $export = $this->completedExport();
        DB::table('silver.exports')->where('export_id', $export->export_id)->update([
            'download_url' => 'https://stale.example.test/dead-session-url',
            'download_url_expires_at' => now()->addHours(20),
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports/{$export->export_id}",
        );

        $response->assertOk();
        $this->assertStringNotContainsString('stale.example.test', (string) $response->json('data.download_url'));
        $this->assertStringContainsString(self::OBJECT_KEY, (string) $response->json('data.download_url'));
        $this->assertEqualsWithDelta(
            now()->addMinutes(5)->getTimestamp(),
            Carbon::parse($response->json('data.download_url_expires_at'))->getTimestamp(),
            5,
        );
    }

    public function test_show_returns_a_session_token_sized_url_that_no_column_would_hold(): void
    {
        // Production signs with ECS task-role credentials, so the presigned URL
        // carries an X-Amz-Security-Token. Configure the REAL s3-exports disk
        // that way (offline: signing needs no network) rather than faking it.
        $this->useRealExportsDiskWithSessionToken(str_repeat('T', 1400));
        $export = $this->completedExport();

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports/{$export->export_id}",
        );

        $response->assertOk();
        $url = (string) $response->json('data.download_url');
        $this->assertGreaterThan(1000, strlen($url), 'precondition: longer than the old varchar(1000)');
        $this->assertStringContainsString('X-Amz-Security-Token=', $url);
        $this->assertStringStartsWith('https://georag-exports-test.s3.ca-central-1.amazonaws.com/', $url);

        // Returned, never stored.
        $this->assertNull(Export::find($export->export_id)->getRawOriginal('download_url'));
    }

    public function test_a_pending_export_has_no_download_link(): void
    {
        $export = Export::create([
            'project_id' => $this->project->project_id,
            'export_type' => 'csv_collars',
            'status' => 'running',
            'filters' => [],
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports/{$export->export_id}",
        );

        $response->assertOk()
            ->assertJsonPath('data.download_url', null)
            ->assertJsonPath('data.download_url_expires_at', null);
    }

    public function test_index_carries_a_fresh_link_for_completed_exports_only(): void
    {
        $done = $this->completedExport();
        $running = Export::create([
            'project_id' => $this->project->project_id,
            'export_type' => 'csv_assays',
            'status' => 'running',
            'filters' => [],
        ]);

        $rows = collect($this->getJson("/api/v1/projects/{$this->project->project_id}/exports")
            ->assertOk()
            ->json('data'))->keyBy('export_id');

        $this->assertStringContainsString(self::OBJECT_KEY, (string) $rows[$done->export_id]['download_url']);
        $this->assertNull($rows[$running->export_id]['download_url']);
    }

    public function test_show_returns_404_for_export_in_wrong_project(): void
    {
        $otherProject = Project::create([
            'project_name' => 'Other Project '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);

        $export = Export::create([
            'project_id' => $otherProject->project_id,
            'export_type' => 'csv_collars',
            'status' => 'pending',
            'filters' => [],
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports/{$export->export_id}",
        );

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // index — list exports
    // -------------------------------------------------------------------------

    public function test_index_returns_exports_for_project(): void
    {
        Export::create([
            'project_id' => $this->project->project_id,
            'export_type' => 'csv_collars',
            'status' => 'pending',
            'filters' => [],
        ]);

        Export::create([
            'project_id' => $this->project->project_id,
            'export_type' => 'csa_bundle',
            'status' => 'completed',
            'filters' => [],
        ]);

        $response = $this->getJson(
            "/api/v1/projects/{$this->project->project_id}/exports",
        );

        $response->assertOk();
        $this->assertGreaterThanOrEqual(2, count($response->json('data')));
    }

    public function test_index_returns_403_for_non_member_project(): void
    {
        // Non-member requests MUST get 403, not 404 — that way the API cannot
        // be used to enumerate which project UUIDs exist.
        $response = $this->getJson(
            '/api/v1/projects/00000000-0000-0000-0000-000000000000/exports',
        );

        $response->assertForbidden();
    }

    // -------------------------------------------------------------------------
    // download — 409 when not completed
    // -------------------------------------------------------------------------

    public function test_download_returns_409_when_export_not_completed(): void
    {
        $export = Export::create([
            'project_id' => $this->project->project_id,
            'export_type' => 'csv_collars',
            'status' => 'pending',
            'filters' => [],
        ]);

        $response = $this->getJson("/api/v1/exports/{$export->export_id}/download");

        $response->assertStatus(409);
    }

    public function test_download_returns_404_for_nonexistent_export(): void
    {
        $response = $this->getJson(
            '/api/v1/exports/00000000-0000-0000-0000-000000000000/download',
        );

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // download — redirect to a URL minted for this request
    // -------------------------------------------------------------------------

    public function test_download_redirects_to_a_url_minted_from_the_object_key(): void
    {
        $export = $this->completedExport();

        $response = $this->get("/api/v1/exports/{$export->export_id}/download");

        $response->assertStatus(302);
        $location = (string) $response->headers->get('Location');
        $this->assertStringContainsString(self::OBJECT_KEY, $location);

        parse_str((string) parse_url($location, PHP_URL_QUERY), $query);
        $this->assertEqualsWithDelta(now()->addMinutes(5)->getTimestamp(), (int) $query['expiration'], 5);
    }

    public function test_download_does_not_follow_a_stored_url_that_is_still_in_date(): void
    {
        // The regression: a stored URL whose recorded expiry was still in the
        // future was redirected to as-is, however long ago its credentials had
        // lapsed.
        $export = $this->completedExport();
        DB::table('silver.exports')->where('export_id', $export->export_id)->update([
            'download_url' => 'https://stale.example.test/dead-session-url',
            'download_url_expires_at' => now()->addHours(20),
        ]);

        $response = $this->get("/api/v1/exports/{$export->export_id}/download");

        $response->assertStatus(302);
        $this->assertStringNotContainsString('stale.example.test', (string) $response->headers->get('Location'));
        $this->assertStringContainsString(self::OBJECT_KEY, (string) $response->headers->get('Location'));
    }

    public function test_download_of_a_completed_export_with_no_object_key_is_404(): void
    {
        $export = $this->completedExport(['minio_path' => null]);

        $this->getJson("/api/v1/exports/{$export->export_id}/download")
            ->assertNotFound()
            ->assertJsonPath('message', 'Export has no stored file.');
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    /**
     * @param array<string, mixed> $overrides
     */
    private function completedExport(array $overrides = []): Export
    {
        return Export::create(array_merge([
            'project_id' => $this->project->project_id,
            'export_type' => 'csv_collars',
            'status' => 'completed',
            'filters' => [],
            'minio_path' => self::OBJECT_KEY,
            'completed_at' => now(),
            'file_count' => 1,
            'total_size_bytes' => 1024,
        ], $overrides));
    }

    /**
     * Point the s3-exports disk at AWS the way production is configured: no
     * endpoint, a region, and session credentials (key + secret + token), as an
     * ECS task role supplies them.
     */
    private function useRealExportsDiskWithSessionToken(string $token): void
    {
        config(['filesystems.disks.s3-exports' => [
            'driver' => 's3',
            'key' => 'ASIAEXAMPLEEXAMPLE',
            'secret' => 'example-secret-access-key',
            'token' => $token,
            'region' => 'ca-central-1',
            'bucket' => 'georag-exports-test',
            'endpoint' => null,
            'use_path_style_endpoint' => false,
            'throw' => false,
        ]]);
        Storage::forgetDisk('s3-exports');
    }
}
