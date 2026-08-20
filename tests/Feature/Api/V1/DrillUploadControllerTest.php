<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
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
        Http::fake([
            '*' => Http::response(['errors' => null], 200),
        ]);
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
     * 2026-08-17 CI-gap audit: this file predates the 2026-07-28 Dagster
     * retirement (trim B2) and, because CI never actually ran the
     * Postgres-gated suite (see phpunit.pgsql.xml's own header /
     * docs/RUNBOOK.md), that drift was never caught. `DrillUploadController
     * ::store()` (line 77-83) now rejects every non-PDF extension with a
     * 422 `retired_pipeline` BEFORE `DrillAssetSelector::select()` is ever
     * called — so the 'dagster' route, and DrillAssetSelector's csv/xlsx
     * keyword-routing branches, are unreachable from this controller.
     * `DrillAssetSelector` and `DagsterGraphQLClient` have no other caller
     * (confirmed via `grep -rln DagsterGraphQLClient app/` and
     * `grep -rn DrillAssetSelector:: app/`), so the previous six
     * Dagster-mock tests plus the "unrouted csv" test were exercising dead
     * code the whole time. Replaced with one test asserting the actual,
     * intended retirement behavior.
     */
    public function test_non_pdf_extension_returns_422_retired_pipeline_without_persisting(): void
    {
        foreach (['collars_2024.csv', 'lithology_log.csv', 'mixed.xlsx', 'legacy.xls'] as $name) {
            $ext = pathinfo($name, PATHINFO_EXTENSION);
            $file = $ext === 'csv'
                ? $this->csv($name)
                : UploadedFile::fake()->createWithContent($name, 'stub-'.$ext);

            $this->actingAs($this->user)
                ->postJson($this->url(), ['file' => $file])
                ->assertStatus(422)
                ->assertJsonPath('error', 'retired_pipeline');
        }

        $this->assertSame(
            0,
            DB::table('bronze.source_files')->where('workspace_id', $this->workspaceId)->count(),
            'a retired-pipeline extension must be rejected before anything is stored',
        );
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
