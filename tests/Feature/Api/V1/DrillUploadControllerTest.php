<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use GuzzleHttp\Promise\PromiseInterface;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\Request as ClientRequest;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * CC-01 Item 1 Slice 1 — DrillUploadController feature coverage.
 *
 * Postgres-only: writes to bronze.source_files which doesn't exist on
 * the SQLite fast suite (the bronze migration is gated on driver=pgsql).
 * Run with `php artisan test -c phpunit.pgsql.xml --filter=DrillUploadControllerTest`.
 */
class DrillUploadControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    private string $workspaceId;

    /**
     * Per-test HTTP stubs, matched before setUp()'s default response.
     *
     * @var array<string, PromiseInterface>
     */
    private array $httpOverrides = [];

    protected function setUp(): void
    {
        parent::setUp();

        // silver.projects.workspace_id is only auto-populated by the
        // phase0 raw-SQL bootstrap in a real deployment (see
        // 2026_08_14_000000/025900) — a migrate-only Postgres test DB has
        // no such trigger, so it must be set explicitly here rather than
        // relying on it being auto-filled. workspace_id isn't fillable on
        // the Project model, so create the workspace + project, then
        // assign via a raw update — same pattern as
        // tests/Feature/Api/V1/IngestProgressControllerTest.php.
        $this->workspaceId = (string) Str::uuid();
        DB::table('silver.workspaces')->insert([
            'workspace_id' => $this->workspaceId,
            'name' => 'Drill Upload Test Workspace',
            'slug' => 'drill-upload-'.substr($this->workspaceId, 0, 8),
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        $this->project = Project::create([
            'project_name' => 'Drill Upload Test '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);
        DB::table('silver.projects')
            ->where('project_id', $this->project->project_id)
            ->update(['workspace_id' => $this->workspaceId]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);

        Storage::fake('s3');

        // Http::fake() MERGES into the stub list and the FIRST matching
        // stub answers. A '*' catch-all registered here is therefore
        // unreachable-past: a test that later faked a 500 for
        // ingest_tabular got this 200 instead and asserted against a
        // success it never asked for. Route through $httpOverrides so a
        // per-test stub is consulted BEFORE the default, not after it.
        Http::fake(function (ClientRequest $request) {
            foreach ($this->httpOverrides as $pattern => $response) {
                if (Str::is($pattern, $request->url())) {
                    return $response;
                }
            }

            return Http::response(['errors' => null], 200);
        });
    }

    private function url(): string
    {
        return "/api/v1/projects/{$this->project->slug}/drill-uploads";
    }

    private function csv(string $name = 'collars.csv', string $content = "hole_id,east,north\nDH001,500000,6000000\n"): UploadedFile
    {
        return UploadedFile::fake()->createWithContent($name, $content);
    }

    private function pdf(string $name = 'report.pdf', ?string $content = null): UploadedFile
    {
        return UploadedFile::fake()->createWithContent(
            $name,
            $content ?? "%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n",
        );
    }

    public function test_unknown_slug_returns_404(): void
    {
        $this->actingAs($this->user)
            ->postJson('/api/v1/projects/this-slug-does-not-exist/drill-uploads', [
                'file' => $this->csv(),
            ])
            ->assertNotFound();
    }

    public function test_non_member_user_is_forbidden(): void
    {
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->postJson($this->url(), ['file' => $this->csv()])
            ->assertForbidden();
    }

    public function test_unsupported_extension_returns_422(): void
    {
        $jpg = UploadedFile::fake()->image('photo.jpg');

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $jpg])
            ->assertStatus(422)
            ->assertJsonPath('error', 'unsupported_extension');
    }

    /**
     * 2026-08-17 CI-gap audit: this file predated the 2026-07-28 Dagster
     * retirement and, because CI never ran the Postgres-gated suite, the
     * drift went unnoticed. The controller then rejected every non-PDF
     * extension with a 422 `retired_pipeline`, and this file was rewritten
     * to assert that rejection.
     *
     * 2026-08-22: the rejection itself was the drift. `ingest_tabular`
     * shipped on 2026-08-20 and UploadController restored CSV/XLSX the same
     * day, so the drill-specific endpoint was the one surface still
     * refusing drill data. These tests cover the restored route.
     */
    public function test_collar_csv_dispatches_to_ingest_tabular_with_its_sheet_type(): void
    {
        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')])
            ->assertStatus(201)
            ->assertJsonPath('route', 'hatchet_tabular')
            ->assertJsonPath('sheet_type', 'collar')
            ->assertJsonPath('dispatch.dispatched', true);

        Http::assertSent(function ($request) {
            return str_contains($request->url(), '/shadow/ingest_tabular/trigger')
                && ($request['sheet_type'] ?? null) === 'collar'
                && ($request['workspace_id'] ?? null) === $this->workspaceId;
        });

        $this->assertSame(
            1,
            DB::table('bronze.source_files')->where('workspace_id', $this->workspaceId)->count(),
            'the upload must still be anchored in bronze.source_files',
        );
    }

    public function test_a_workbook_is_dispatched_without_a_sheet_type(): void
    {
        // A workbook holds several tables. Pinning one type would make
        // ingest_tabular apply it to every sheet instead of classifying
        // each — so the key is omitted rather than sent as null.
        $xlsx = UploadedFile::fake()->createWithContent('mixed.xlsx', 'stub-xlsx');

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $xlsx])
            ->assertStatus(201)
            ->assertJsonPath('route', 'hatchet_tabular')
            ->assertJsonPath('sheet_type', null);

        Http::assertSent(function ($request) {
            return str_contains($request->url(), '/shadow/ingest_tabular/trigger')
                && ! array_key_exists('sheet_type', $request->data());
        });
    }

    public function test_a_csv_with_no_filename_hint_is_still_dispatched(): void
    {
        // The old behaviour was 'unrouted': stored, never processed, 201.
        // ingest_tabular classifies from the header row, so there is no
        // reason to drop the file on the floor.
        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('data.csv')])
            ->assertStatus(201)
            ->assertJsonPath('route', 'hatchet_tabular')
            ->assertJsonPath('sheet_type', null)
            ->assertJsonPath('dispatch.dispatched', true);
    }

    public function test_a_failed_tabular_dispatch_is_a_502_not_a_quiet_201(): void
    {
        // The file IS stored, so a 201 reads as unqualified success while
        // the only signal is `dispatch.dispatched` three levels deep.
        $this->httpOverrides = [
            '*ingest_tabular*' => Http::response(['detail' => 'nope'], 500),
        ];

        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('surveys.csv')])
            ->assertStatus(502)
            ->assertJsonPath('error', 'ingestion_dispatch_failed')
            ->assertJsonPath('dispatch.dispatched', false);
    }

    public function test_source_epsg_reaches_the_ingest_tabular_trigger(): void
    {
        // IngestTabularInput has accepted `source_epsg` since it shipped and
        // has never once been sent one, so every drill file uploaded through
        // this route has silently taken DEFAULT_SOURCE_EPSG = 32613 (UTM
        // 13N). Correct in Saskatchewan; a continent out for the Alaskan
        // collars this override exists for (26904 = NAD83 / UTM 4N).
        $this->actingAs($this->user)
            ->postJson($this->url(), [
                'file' => $this->csv('collars_unga.csv'),
                'source_epsg' => 26904,
            ])
            ->assertStatus(201)
            ->assertJsonPath('source_epsg', 26904)
            ->assertJsonPath('dispatch.source_epsg', 26904);

        Http::assertSent(function ($request) {
            return str_contains($request->url(), '/shadow/ingest_tabular/trigger')
                && ($request['source_epsg'] ?? null) === 26904
                // Adding one hint must not displace the other.
                && ($request['sheet_type'] ?? null) === 'collar';
        });
    }

    public function test_source_epsg_is_omitted_when_not_supplied(): void
    {
        // Absence and null are not the same message. ingest_tabular reads a
        // missing key as "no operator assertion, use the default"; sending
        // the key as null says nothing extra and invites a future reader to
        // treat it as an explicit choice.
        $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->csv('collars_2024.csv')])
            ->assertStatus(201)
            ->assertJsonMissingPath('source_epsg');

        Http::assertSent(function ($request) {
            return str_contains($request->url(), '/shadow/ingest_tabular/trigger')
                && ! array_key_exists('source_epsg', $request->data());
        });
    }

    public function test_duplicate_sha256_returns_existing_row_without_re_uploading(): void
    {
        $payload = "%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n";
        $first = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->pdf('report_a.pdf', $payload)])
            ->assertCreated();

        // Same content under a different filename — SHA matches, so we
        // expect a 200 + duplicate=true pointing at the original row.
        $second = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $this->pdf('report_b.pdf', $payload)])
            ->assertOk()
            ->assertJsonPath('duplicate', true);

        $this->assertSame($first->json('source_file_id'), $second->json('source_file_id'));
        $this->assertCount(1, DB::table('bronze.source_files')
            ->where('workspace_id', $this->workspaceId)
            ->get(), 'a duplicate SHA must not create a second row');
    }

    public function test_persisted_mime_type_is_server_sniffed_not_client_declared(): void
    {
        // Security fix 2026-08-14 (MED): bronze.source_files.mime_type used
        // to store the attacker-controlled client-declared MIME. Upload a
        // real PDF while declaring a bogus client mime and assert the
        // sniffed value wins.
        $path = tempnam(sys_get_temp_dir(), 'georag_pdf_');
        file_put_contents(
            $path,
            "%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n",
        );
        $file = new UploadedFile($path, 'well_report.pdf', 'text/plain', null, true);

        $response = $this->actingAs($this->user)
            ->postJson($this->url(), ['file' => $file]);

        // 201 when the (faked) FastAPI dispatch succeeds, 502 when it does
        // not — either way the bronze row must already be persisted.
        $this->assertContains($response->status(), [201, 502]);

        $sourceFileId = $response->json('source_file_id');
        $this->assertNotEmpty($sourceFileId);

        $row = DB::table('bronze.source_files')->where('id', $sourceFileId)->first();
        $this->assertNotNull($row);
        $this->assertSame(
            'application/pdf',
            $row->mime_type,
            'mime_type must come from server-side content sniffing, not the client-declared value',
        );
    }
}
