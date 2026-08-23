<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * `source_epsg` validation on POST /api/v1/projects/{slug}/drill-uploads.
 *
 * Split out from DrillUploadControllerTest deliberately. That suite is
 * Postgres-only because it asserts on bronze.source_files rows, and the
 * hand-ordered phpunit.pgsql.xml file list is the only thing that runs it.
 * Validation, though, happens before the workspace lookup and before
 * anything is written to bronze, so the range checks run perfectly well on
 * the SQLite fast suite — and a rule that only ever runs in the slow,
 * separately-invoked suite is a rule that stops being checked.
 *
 * The forwarding half of the same field lives in DrillUploadControllerTest
 * (Postgres), because reaching the dispatcher requires the bronze row.
 */
class DrillUploadSourceEpsgValidationTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();

        $this->project = Project::create([
            'project_name' => 'Drill EPSG Validation '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);
        DB::table('silver.projects')
            ->where('project_id', $this->project->project_id)
            ->update(['workspace_id' => (string) Str::uuid()]);
        $this->project->refresh();

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);

        Storage::fake('s3');
    }

    private function url(): string
    {
        return "/api/v1/projects/{$this->project->slug}/drill-uploads";
    }

    private function csv(): UploadedFile
    {
        return UploadedFile::fake()->createWithContent(
            'collars.csv',
            "hole_id,east,north\nDH001,400797,6117305\n",
        );
    }

    public function test_source_epsg_below_1024_is_rejected_with_the_platform_wording(): void
    {
        $response = $this->actingAs($this->user)
            ->postJson($this->url(), [
                'file' => $this->csv(),
                'source_epsg' => 1023,
            ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['source_epsg']);

        $this->assertSame(
            'EPSG codes must be in the range 1024-32767.',
            $response->json('errors.source_epsg.0'),
            'This controller validates inline too, so the message has to be '
                .'passed to validate() explicitly to match StoreQueryRequest.',
        );
    }

    public function test_source_epsg_above_32767_is_rejected(): void
    {
        // 32767 is the DB CHECK bound on crs_epsg_native / crs_epsg, not a
        // number picked here. Letting a larger code through moves the
        // failure from a 422 at the door to a whole-file INSERT failure at
        // persist time.
        $this->actingAs($this->user)
            ->postJson($this->url(), [
                'file' => $this->csv(),
                'source_epsg' => 32768,
            ])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['source_epsg']);
    }

    public function test_a_crs_string_is_refused(): void
    {
        // The wire type is an integer on every surface. Accepting
        // 'EPSG:26904' here would give the platform two spellings of one
        // concept across two ingest paths that share a UI.
        $this->actingAs($this->user)
            ->postJson($this->url(), [
                'file' => $this->csv(),
                'source_epsg' => 'EPSG:26904',
            ])
            ->assertUnprocessable()
            ->assertJsonValidationErrors(['source_epsg']);
    }

    public function test_a_rejected_source_epsg_stores_nothing(): void
    {
        // Validation runs before the bronze put, so a refused override must
        // not leave an object behind for the Tier-1 sweep to find orphaned.
        $this->actingAs($this->user)
            ->postJson($this->url(), [
                'file' => $this->csv(),
                'source_epsg' => 4,
            ])
            ->assertUnprocessable();

        $this->assertEmpty(
            Storage::disk('s3')->allFiles(),
            'a 422 on validation must happen before anything is written',
        );
    }
}
