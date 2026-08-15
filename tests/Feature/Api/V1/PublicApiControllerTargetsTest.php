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
 * Regression coverage for PublicApiController::targets()
 * (GET /api/v1/targets/{project_id}).
 *
 * Security fix 2026-08-15, alongside
 * 2026_08_15_020100_close_rls_admin_escape_hatch_second_pass: this
 * endpoint gated cross-tenant reads via hasProjectAccess() but never
 * bound the `app.workspace_id` RLS GUC before querying
 * `targeting.target_recommendations`. That was harmless while the
 * table's RLS policy was fail-open (unset GUC admitted all rows), but
 * once the table flips to fail-closed, an unbound GUC would 0-row every
 * legitimate caller too — not just attackers. The fix wraps the query in
 * SetsWorkspaceRlsContext::withWorkspaceRls() (same pattern as the
 * 2026-08-14 CitationController IDOR fix).
 *
 * The critical case this suite exists to catch: a member of the OWNING
 * workspace must still see their own target recommendations after the
 * RLS flip (test_returns_recommendations_for_owning_workspace_member).
 * Without the GUC-bind fix, that case alone would regress to an empty
 * `items: []` — passing every "cross-tenant" IDOR check while silently
 * breaking the feature for everyone.
 */
class PublicApiControllerTargetsTest extends TestCase
{
    use RefreshDatabase;

    private User $userA;

    private User $userB;

    private string $workspaceA;

    private string $workspaceB;

    private string $projectAId;

    private string $recommendationId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceA = (string) Str::uuid();
        $this->workspaceB = (string) Str::uuid();

        if (DB::connection()->getDriverName() !== 'sqlite') {
            DB::table('silver.workspaces')->insert([
                [
                    'workspace_id' => $this->workspaceA,
                    'name' => 'Targets Test Workspace A',
                    'slug' => 'targets-test-a-'.substr($this->workspaceA, 0, 8),
                    'created_at' => now(),
                    'updated_at' => now(),
                ],
                [
                    'workspace_id' => $this->workspaceB,
                    'name' => 'Targets Test Workspace B',
                    'slug' => 'targets-test-b-'.substr($this->workspaceB, 0, 8),
                    'created_at' => now(),
                    'updated_at' => now(),
                ],
            ]);
        }

        $this->userA = User::factory()->create();
        $projectA = Project::create([
            'project_name' => 'Workspace A Project '.uniqid(),
            'orientation_reference' => 'BOH',
        ]);
        $this->userA->projects()->attach($projectA->project_id, ['role' => 'owner']);
        DB::table('silver.projects')
            ->where('project_id', $projectA->project_id)
            ->update(['workspace_id' => $this->workspaceA]);
        $this->projectAId = (string) $projectA->project_id;

        $this->userB = User::factory()->create();
        $projectB = Project::create([
            'project_name' => 'Workspace B Project '.uniqid(),
            'orientation_reference' => 'BOH',
        ]);
        $this->userB->projects()->attach($projectB->project_id, ['role' => 'owner']);
        DB::table('silver.projects')
            ->where('project_id', $projectB->project_id)
            ->update(['workspace_id' => $this->workspaceB]);

        // A target recommendation owned by workspace A / project A —
        // only exercised under Postgres (targeting.* raw-SQL schema is
        // no-op'd on SQLite, matching the CitationControllerIDORTest
        // pattern for silver.reports). Builds the full FK chain
        // (target_models -> target_model_versions -> target_candidate_zones
        // -> target_scores -> target_recommendations) the schema requires.
        if (DB::connection()->getDriverName() !== 'sqlite') {
            $this->recommendationId = (string) Str::uuid();
            $runId = (string) Str::uuid();
            $zoneId = (string) Str::uuid();
            $scoreId = (string) Str::uuid();
            $modelId = (string) Str::uuid();
            $versionId = (string) Str::uuid();

            DB::table('targeting.target_models')->insert([
                'target_model_id' => $modelId,
                'slug' => 'test_model_'.substr(str_replace('-', '', $modelId), 0, 8),
                'display_name' => 'Test Model',
                'commodity_primary' => 'gold',
                'created_at' => now(),
            ]);
            DB::table('targeting.target_model_versions')->insert([
                'version_id' => $versionId,
                'target_model_id' => $modelId,
                'version' => 1,
                'scoring_kind' => 'weighted',
                'created_at' => now(),
            ]);
            DB::statement(
                'INSERT INTO targeting.target_candidate_zones
                    (zone_id, workspace_id, project_id, target_model_id, run_id, zone_geom, created_at)
                 VALUES (?::uuid, ?::uuid, ?::uuid, ?::uuid, ?::uuid,
                    ST_GeomFromText(?, 4326), now())',
                [
                    $zoneId, $this->workspaceA, $this->projectAId, $modelId, $runId,
                    'POLYGON((0 0, 0 1, 1 1, 1 0, 0 0))',
                ],
            );
            DB::table('targeting.target_scores')->insert([
                'score_id' => $scoreId,
                'zone_id' => $zoneId,
                'workspace_id' => $this->workspaceA,
                'model_version_id' => $versionId,
                'aggregate_score' => 0.8,
                'computed_at' => now(),
            ]);
            DB::table('targeting.target_recommendations')->insert([
                'recommendation_id' => $this->recommendationId,
                'workspace_id' => $this->workspaceA,
                'project_id' => $this->projectAId,
                'run_id' => $runId,
                'zone_id' => $zoneId,
                'score_id' => $scoreId,
                'rank' => 1,
                'explanation_markdown' => 'Zone Z-1 ranks first on alteration overlap.',
                'created_at' => now(),
            ]);
        }
    }

    public function test_unauthenticated_targets_returns_401(): void
    {
        $response = $this->getJson("/api/v1/targets/{$this->projectAId}");

        $response->assertUnauthorized();
    }

    public function test_targets_for_project_without_access_returns_404(): void
    {
        $this->actingAs($this->userB, 'sanctum');

        $response = $this->getJson("/api/v1/targets/{$this->projectAId}");

        $response->assertNotFound();
    }

    /**
     * The regression this test file exists for: a legitimate member of
     * the OWNING workspace must still see their target recommendations
     * now that targeting.target_recommendations is fail-closed. Without
     * the withWorkspaceRls() fix in PublicApiController::targets(), this
     * would return `items: []` — the RLS policy silently filtering out
     * every row because the GUC was never bound, even for the rightful
     * owner.
     */
    public function test_returns_recommendations_for_owning_workspace_member(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('targeting.* is Postgres-only (raw SQL schema).');
        }

        $this->actingAs($this->userA, 'sanctum');

        $response = $this->getJson("/api/v1/targets/{$this->projectAId}");

        $response->assertOk()
            ->assertJsonPath('project_id', $this->projectAId)
            ->assertJsonCount(1, 'items')
            ->assertJsonPath('items.0.id', $this->recommendationId);
    }
}
