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
 * The two extension-list changes: MapInfo on the spatial path, standalone
 * dBASE on the tabular one.
 *
 * Both are one-line edits to a const, and both have a wrong version that
 * looks identical in review:
 *
 *   - MapInfo: adding the SIDECARS (.dat/.map/.id/.ind/.mid) alongside the
 *     entry points. A .mid opens directly as a dataset, so a MIF/MID pair
 *     uploaded member-by-member would be ingested twice; and '.dat' is
 *     already claimed by the retired `xyz` category, so accepting it here
 *     would start routing stray XYZ grids into the spatial parser.
 *   - dBASE: putting '.dbf' in a category that pins a sheet_type, or in the
 *     spatial category. A standalone .dbf has no geometry — reaching the
 *     spatial parser it dies on `'DataFrame' object has no attribute 'crs'`
 *     — and its contents are not guessable from the extension, so the right
 *     home is the one tabular category that sends no sheet_type hint.
 *
 * These assertions are about the routing decision, not the file's contents,
 * so they run on the SQLite fast suite alongside the rest of store().
 */
class UploadCategoryExtensionsTest extends TestCase
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
            'project_name' => 'Category Extensions '.uniqid(),
            'crs_datum' => 'EPSG:32613',
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

            return Http::response(['workflow_run_id' => 'test-workflow-run-id'], 202);
        });
    }

    private function uploadUrl(): string
    {
        return "/api/v1/projects/{$this->project->project_id}/upload";
    }

    private function triggeredWorkflow(): ?string
    {
        foreach ($this->captured as $call) {
            if (preg_match('#/shadow/([a-z_]+)/trigger#', $call['url'], $m) === 1) {
                return $m[1];
            }
        }

        return null;
    }

    /**
     * @return array<string, list<string>>
     */
    private function publishedCategories(): array
    {
        $response = $this->actingAs($this->user)->getJson('/api/v1/upload/categories');
        $response->assertOk();

        /** @var array<string, list<string>> $categories */
        $categories = $response->json('categories');

        return $categories;
    }

    // ── R5: MapInfo ──────────────────────────────────────────────────────

    public function test_mapinfo_entry_points_are_accepted_as_spatial(): void
    {
        $spatial = $this->publishedCategories()['spatial'];

        $this->assertContains('tab', $spatial);
        $this->assertContains('mif', $spatial);
    }

    public function test_mapinfo_sidecars_are_not_standalone_spatial_uploads(): void
    {
        $spatial = $this->publishedCategories()['spatial'];

        foreach (['dat', 'map', 'id', 'ind', 'mid'] as $sidecar) {
            $this->assertNotContains(
                $sidecar,
                $spatial,
                "'.{$sidecar}' is a MapInfo sidecar, not an entry point. A .mid "
                    .'opens directly as a dataset, so accepting it would ingest a '
                    ."MIF/MID pair twice; '.dat' is claimed by the retired xyz "
                    .'category and would start routing XYZ grids to the spatial parser.',
            );
        }
    }

    public function test_a_tab_upload_dispatches_to_ingest_spatial(): void
    {
        $tab = UploadedFile::fake()->createWithContent(
            'veins.tab',
            "!table\n!version 300\n!charset WindowsLatin1\n\nDefinition Table\n",
        );

        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), ['file' => $tab, 'category' => 'spatial'])
            ->assertCreated();

        $this->assertSame('ingest_spatial', $this->triggeredWorkflow());
    }

    // ── R6: standalone dBASE ─────────────────────────────────────────────

    public function test_the_tables_category_has_a_workflow(): void
    {
        // A CATEGORIES entry with no GEOLOGY_WORKFLOWS entry answers 201,
        // writes the object and dispatches nothing — the retired-category
        // bug, reproduced by omitting one line. The only way to see it is to
        // upload and check something was actually triggered.
        $categories = $this->publishedCategories();
        $this->assertArrayHasKey('tables', $categories);
        // `.dat` joined `.dbf` on 2026-08-25: a MapInfo attribute half IS a
        // dBASE file and reads standalone once its master is absent.
        // `.mdb`/`.accdb` followed the same day — an Access database is a
        // CONTAINER of tables and fans out to one attribute_tables layer per
        // Access table. All four go to ingest_tabular, which is what this test
        // is really guarding.
        $this->assertSame(['dbf', 'dat', 'mdb', 'accdb'], $categories['tables']);
        $this->assertNotContains(
            'dbf',
            $categories['excel'],
            'a dBASE table is not a workbook; it has its own category and label',
        );
    }

    public function test_a_standalone_dbf_is_accepted_and_routed_to_the_tabular_path(): void
    {
        // Before this it was a 422 at the door: '.dbf' was in no live
        // category at all, so an attribute table delivered without its .shp
        // could not be uploaded by any route.
        $dbf = UploadedFile::fake()->createWithContent(
            'silicification.dbf',
            "\x03\x00\x00\x00".str_repeat("\x00", 60),
        );

        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), ['file' => $dbf, 'category' => 'tables'])
            ->assertCreated();

        $this->assertSame('ingest_tabular', $this->triggeredWorkflow());
    }

    public function test_a_dbf_carries_no_sheet_type_hint(): void
    {
        // The reason '.dbf' is not in collars/surveys/lithology/samples:
        // nothing about the extension says which drill table it is, and the
        // four hinted categories would pin one anyway — the same mistake the
        // sheet_type comment in dispatchGeologyIngest() warns about for
        // workbooks.
        //
        // And the reason this asserts on ABSENCE rather than on null: the
        // hint used to be gated by `$category !== 'excel'`, which would have
        // sent `sheet_type: null` for every upload in this new category the
        // moment it was added.
        $dbf = UploadedFile::fake()->createWithContent(
            'attributes.dbf',
            "\x03\x00\x00\x00".str_repeat("\x00", 60),
        );

        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), ['file' => $dbf, 'category' => 'tables'])
            ->assertCreated();

        $payload = $this->captured[0]['data'] ?? [];
        $this->assertArrayNotHasKey('sheet_type', $payload);
    }

    public function test_dbf_is_not_a_standalone_spatial_upload(): void
    {
        // A .dbf beside a same-stem .shp is a shapefile sidecar and travels
        // inside the bundle zip. Sent to the spatial parser on its own it
        // returns a plain DataFrame and dies on `.crs`.
        $spatial = $this->publishedCategories()['spatial'];
        $this->assertNotContains('dbf', $spatial);

        $dbf = UploadedFile::fake()->createWithContent(
            'attributes.dbf',
            "\x03\x00\x00\x00".str_repeat("\x00", 60),
        );

        $this->actingAs($this->user)
            ->postJson($this->uploadUrl(), ['file' => $dbf, 'category' => 'spatial'])
            ->assertUnprocessable();
    }

    // ── Neither change may disturb the retired list ──────────────────────

    public function test_the_retired_xyz_category_still_owns_dat(): void
    {
        $response = $this->actingAs($this->user)->getJson('/api/v1/upload/categories');
        $response->assertOk();

        $this->assertContains('dat', $response->json('retired.xyz'));
    }
}
