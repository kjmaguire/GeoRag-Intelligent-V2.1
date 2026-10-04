<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Services\IngestionSnapshot;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * IngestionSnapshot::build() reads reports and progress rows with a cap, newest
 * first, and says so in the payload when a section was cut.
 *
 *   php artisan test -c phpunit.pgsql.xml --filter=IngestionSnapshotCapTest
 */
final class IngestionSnapshotCapTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private Project $project;

    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW()) ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Cap Workspace', 'cap-'.substr($this->workspaceId, 0, 8)],
        );
        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
        Storage::fake('s3-bronze');
    }

    private function snapshot(): array
    {
        return app(IngestionSnapshot::class)->build((string) $this->project->project_id, $this->workspaceId);
    }

    public function test_a_small_project_is_not_flagged_truncated(): void
    {
        $snapshot = $this->snapshot();

        $this->assertFalse($snapshot['truncated']);
        $this->assertSame(['reports' => false, 'progress' => false, 'uploads' => false], $snapshot['truncated_sections']);
    }

    public function test_reports_are_capped_to_the_newest_rows_and_flagged(): void
    {
        $cap = IngestionSnapshot::MAX_ROWS_PER_SECTION;
        DB::statement(
            "INSERT INTO silver.reports (report_id, workspace_id, project_id, title, page_count, created_at, updated_at)
             SELECT gen_random_uuid(), ?::uuid, ?::uuid, 'Report ' || g, 1, now() - (g || ' minutes')::interval, now()
             FROM generate_series(1, ?) AS g",
            [$this->workspaceId, $this->project->project_id, $cap + 20],
        );

        $snapshot = $this->snapshot();

        $this->assertCount($cap, $snapshot['completed']);
        $this->assertTrue($snapshot['truncated']);
        $this->assertTrue($snapshot['truncated_sections']['reports']);
        $this->assertFalse($snapshot['truncated_sections']['progress']);
        // Newest first: "Report 1" is the most recent, "Report {cap+20}" the oldest and is cut.
        $titles = array_column($snapshot['completed'], 'title');
        $this->assertContains('Report 1', $titles);
        $this->assertNotContains('Report '.($cap + 20), $titles);
    }

    public function test_progress_rows_are_capped_to_the_newest_runs_and_flagged(): void
    {
        $cap = IngestionSnapshot::MAX_ROWS_PER_SECTION;
        DB::statement(
            "INSERT INTO silver.ingest_progress (workspace_id, project_id, minio_key, filename, current_step, step_index, total_steps, status, started_at, updated_at)
             SELECT ?::uuid, ?::uuid, 'reports/' || ?::text || '/f' || g || '.pdf', 'f' || g || '.pdf', 'completed', 5, 5, 'completed',
                    now() - (g || ' minutes')::interval, now() - (g || ' minutes')::interval
             FROM generate_series(1, ?) AS g",
            [$this->workspaceId, $this->project->project_id, $this->project->project_id, $cap + 20],
        );

        $snapshot = $this->snapshot();

        $this->assertSame($cap, $snapshot['totals']['files']);
        $this->assertTrue($snapshot['truncated_sections']['progress']);
        $this->assertTrue($snapshot['truncated']);
    }
}
