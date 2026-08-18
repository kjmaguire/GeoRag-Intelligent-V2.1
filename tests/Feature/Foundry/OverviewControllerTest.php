<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * OverviewController — the project landing page.
 *
 * Regression coverage for the 2026-08-18 fix: the REPORTS KPI was built from
 * an unfiltered `DB::table('silver.reports')->count()` while every other count
 * on the page was scoped by project_id. With a single project that reads
 * correctly by coincidence; add a second project and both Overviews report the
 * same corpus size. It also fed $nextAction, whose "Connect your first data
 * source" branch is gated on $reportsCount === 0 — unreachable once ANY
 * project in the database has ingested a report.
 *
 * Postgres-only: silver.reports/silver.workspaces live in the pgsql test DB.
 *   php artisan test -c phpunit.pgsql.xml --filter=OverviewControllerTest
 */
final class OverviewControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        $this->workspaceId = (string) Str::uuid();

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Overview Test Workspace', 'overview-'.substr($this->workspaceId, 0, 8)],
        );
    }

    private function makeProject(): Project
    {
        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $project->project_id => ['role' => 'owner'],
        ]);

        return $project;
    }

    private function insertReport(Project $project, string $title): void
    {
        DB::table('silver.reports')->insert([
            'report_id' => (string) Str::uuid(),
            'workspace_id' => $this->workspaceId,
            'project_id' => $project->project_id,
            'title' => $title,
            'page_count' => 10,
            'created_at' => now(),
            'updated_at' => now(),
        ]);
    }

    /** Pull the numeric value out of a named KPI tile. */
    private function kpiValue(AssertableInertia $page, string $label): string
    {
        /** @var list<array{label: string, value: string}> $kpis */
        $kpis = $page->toArray()['props']['kpis'];
        foreach ($kpis as $kpi) {
            if ($kpi['label'] === $label) {
                return $kpi['value'];
            }
        }

        $this->fail("KPI {$label} not present on the Overview page");
    }

    public function test_reports_kpi_counts_only_this_projects_reports(): void
    {
        $mine = $this->makeProject();
        $other = $this->makeProject();

        $this->insertReport($mine, 'Mine A');
        $this->insertReport($mine, 'Mine B');
        $this->insertReport($other, 'Someone else A');
        $this->insertReport($other, 'Someone else B');
        $this->insertReport($other, 'Someone else C');

        $response = $this->actingAs($this->user)->get('/projects/'.$mine->slug);

        $response->assertStatus(200);
        $response->assertInertia(function (AssertableInertia $page) {
            // 2, not 5 — the other project's three reports must not be counted.
            $this->assertSame('2', $this->kpiValue($page, 'REPORTS'));
        });
    }

    public function test_project_with_no_reports_reports_zero_despite_other_projects(): void
    {
        $empty = $this->makeProject();
        $stocked = $this->makeProject();

        $this->insertReport($stocked, 'Not mine');

        $response = $this->actingAs($this->user)->get('/projects/'.$empty->slug);

        $response->assertStatus(200);
        $response->assertInertia(function (AssertableInertia $page) {
            $this->assertSame('0', $this->kpiValue($page, 'REPORTS'));
        });
    }

    public function test_empty_project_is_offered_the_cold_start_next_action(): void
    {
        // The bug's second effect: with an unscoped count, a brand-new project
        // could never reach the $reportsCount === 0 branch once any other
        // project had ingested anything, so a genuinely empty project was
        // never told to connect a data source.
        $empty = $this->makeProject();
        $stocked = $this->makeProject();
        $this->insertReport($stocked, 'Not mine');

        $response = $this->actingAs($this->user)->get('/projects/'.$empty->slug);

        $response->assertStatus(200);
        $response->assertInertia(
            fn (AssertableInertia $page) => $page->where(
                'next_action.title',
                'Connect your first data source',
            )
        );
    }
}
