<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * Regression coverage for PublicApiController::reports()
 * (GET /api/v1/reports).
 *
 * Security fix 2026-08-16: this endpoint had NO tenancy scoping at all —
 * any authenticated user could list every tenant's reports (title,
 * company, commodity, region, filing_date). Confirmed via a full-app
 * review; the codebase's own
 * 2026_08_15_030000_close_rls_admin_escape_hatch_third_pass migration
 * had already flagged it by name as a "pre-existing cross-tenant IDOR
 * bug independent of RLS" without fixing it. The fix scopes the query to
 * the caller's project memberships, same pattern as
 * ProjectController::index().
 */
class PublicApiControllerReportsTest extends TestCase
{
    use RefreshDatabase;

    private User $userA;

    private Project $projectA;

    private Project $projectB;

    private string $reportAId;

    private string $reportBId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->userA = User::factory()->create();
        $userB = User::factory()->create();

        $this->projectA = Project::create([
            'project_name' => 'Project A '.uniqid(),
            'orientation_reference' => 'BOH',
        ]);
        $this->userA->projects()->attach($this->projectA->project_id, ['role' => 'owner']);

        $this->projectB = Project::create([
            'project_name' => 'Project B '.uniqid(),
            'orientation_reference' => 'BOH',
        ]);
        $userB->projects()->attach($this->projectB->project_id, ['role' => 'owner']);

        $this->reportAId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $this->reportAId,
            'project_id' => $this->projectA->project_id,
            'title' => 'Project A Technical Report',
            'company' => 'Tenant A Mining Corp',
            'commodity' => 'gold',
            'created_at' => now(),
            'updated_at' => now(),
        ]);

        $this->reportBId = (string) Str::uuid();
        DB::table('silver.reports')->insert([
            'report_id' => $this->reportBId,
            'project_id' => $this->projectB->project_id,
            'title' => 'Confidential Report for Project B',
            'company' => 'Tenant B Mining Corp',
            'commodity' => 'uranium',
            'created_at' => now(),
            'updated_at' => now(),
        ]);
    }

    public function test_unauthenticated_reports_returns_401(): void
    {
        $this->getJson('/api/v1/reports')->assertUnauthorized();
    }

    public function test_user_only_sees_reports_from_own_projects(): void
    {
        $response = $this->actingAs($this->userA, 'sanctum')
            ->getJson('/api/v1/reports')
            ->assertOk();

        $ids = collect($response->json('items'))->pluck('id')->all();

        $this->assertContains($this->reportAId, $ids, 'Own project report must be visible.');
        $this->assertNotContains($this->reportBId, $ids, 'Other tenant\'s report must not leak.');
    }

    public function test_user_with_no_projects_sees_no_reports(): void
    {
        $userC = User::factory()->create();

        $response = $this->actingAs($userC, 'sanctum')
            ->getJson('/api/v1/reports')
            ->assertOk();

        $this->assertSame(0, $response->json('count'));
        $this->assertSame([], $response->json('items'));
    }
}
