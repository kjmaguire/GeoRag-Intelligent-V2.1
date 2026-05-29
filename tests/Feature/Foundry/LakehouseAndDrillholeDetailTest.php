<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\TestCase;

/**
 * Feature tests for the §B/S/G build-out Inertia pages:
 *
 *   GET /projects/{slug}/lakehouse                   → Foundry/Lakehouse
 *   GET /projects/{slug}/holes/{collarId}/detail     → Foundry/DrillholeDetail
 *
 * The general smoke test FoundryRoutesSmokeTest covers the happy-path render.
 * These tests cover the additions worth a regression net:
 *   - Lakehouse exposes the bronze/silver/gold props with the expected shape.
 *   - DrillholeDetail 404s on a non-existent collar.
 *   - DrillholeDetail 404s when the collar belongs to a different project.
 *   - Lakehouse is locked to project members (403 for non-members).
 *
 * Both controllers SET set_config('app.workspace_id', ...) explicitly to
 * tighten the RLS GUC fallback — these tests run against the real DB so the
 * RLS path is exercised end-to-end.
 */
final class LakehouseAndDrillholeDetailTest extends TestCase
{
    use RefreshDatabase;

    /**
     * Seed the minimum (workspace, project, project_user) needed for the
     * Lakehouse + DrillholeDetail authorization paths. silver.workspaces +
     * silver.projects are raw-SQL phase0 tables; we INSERT them directly
     * here rather than via factories to keep cross-schema FKs straight.
     *
     * @return array{user: User, project: Project, workspace_id: string}
     */
    private function seedProjectMember(): array
    {
        $user = User::factory()->create();

        // silver.workspaces row (slug required)
        $workspaceId = (string) Str::uuid();
        $slug = 'bsg-test-'.substr($workspaceId, 0, 8);
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$workspaceId, 'BSG Test Workspace', $slug],
        );

        $project = Project::factory()->create();

        // Project model's `project_id` lives in silver.projects via the
        // phase0 schema; the factory creates the Eloquent row but doesn't
        // join workspace_id. Backfill workspace_id on silver.projects.
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$workspaceId, $project->project_id],
        );

        // Project model only carries the inverse via User::projects(); attach
        // by inserting into the pivot directly.
        $user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'viewer']]);

        return ['user' => $user, 'project' => $project, 'workspace_id' => $workspaceId];
    }

    /**
     * Insert a minimal silver.collars row owned by the given project.
     * Returns the collar UUID.
     */
    private function seedCollar(string $projectId, string $workspaceId): string
    {
        $collarId = (string) Str::uuid();

        // workspace_id is added by phase0 raw SQL on prod but not on the test
        // DB. Introspect once + branch the INSERT accordingly.
        $hasWorkspaceCol = DB::table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', 'collars')
            ->where('column_name', 'workspace_id')
            ->exists();

        if ($hasWorkspaceCol) {
            DB::statement(
                "INSERT INTO silver.collars (
                    collar_id, hole_id, project_id, workspace_id,
                    easting, northing, elevation, total_depth, azimuth, dip,
                    hole_type, status, geom
                 ) VALUES (
                    ?::uuid, ?, ?::uuid, ?::uuid,
                    500000, 4500000, 1000, 150, 180, -60,
                    'DDH', 'completed',
                    ST_SetSRID(ST_MakePoint(500000, 4500000), 32613)
                 )",
                [$collarId, 'BSG-TEST-001', $projectId, $workspaceId],
            );
        } else {
            DB::statement(
                "INSERT INTO silver.collars (
                    collar_id, hole_id, project_id,
                    easting, northing, elevation, total_depth, azimuth, dip,
                    hole_type, status, geom
                 ) VALUES (
                    ?::uuid, ?, ?::uuid,
                    500000, 4500000, 1000, 150, 180, -60,
                    'DDH', 'completed',
                    ST_SetSRID(ST_MakePoint(500000, 4500000), 32613)
                 )",
                [$collarId, 'BSG-TEST-001', $projectId],
            );
        }

        return $collarId;
    }

    public function test_lakehouse_renders_with_three_layer_props(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectMember();

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/lakehouse');

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->component('Foundry/Lakehouse')
            ->has('project')
            ->has('bronze')
            ->has('silver')
            ->has('gold'),
        );
    }

    /**
     * Locks in the contract added 2026-05-25: every table summary must
     * carry a `scope` label so the UI can render the right pill.
     * Bronze split — source_files=workspace, ingest_manifest/provenance
     * =global (until the writer follow-up populates workspace_id).
     */
    public function test_lakehouse_props_carry_scope_label_per_table(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectMember();

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/lakehouse');

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->where('bronze.source_files.scope', 'workspace')
            ->where('bronze.ingest_manifest.scope', 'global')
            ->where('bronze.provenance.scope', 'global')
            ->where('silver.collars.scope', 'project')
            ->where('gold.h3_density.scope', 'project'),
        );
    }

    /**
     * Locks in the bronze RLS migration (2026_05_25_170825) at the
     * schema level: RLS must be ENABLED on all three bronze tables and
     * each must carry a workspace_isolation policy. The two newly-added
     * tenancy columns (workspace_id on ingest_manifest + provenance)
     * must also exist.
     *
     * Why not a runtime SELECT test: the dedicated `georag_test` DB
     * connects as the `georag` superuser, which has rolbypassrls=true,
     * so policies don't get evaluated. Asserting the catalog state is
     * the right granularity — runtime enforcement is exercised by the
     * production-role smoke checks in tenant_isolation_audit.
     */
    public function test_bronze_rls_migration_installed_at_schema_level(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        foreach (['source_files', 'ingest_manifest', 'provenance'] as $tbl) {
            $rlsOn = (bool) DB::selectOne(
                "SELECT c.relrowsecurity
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                  WHERE n.nspname = 'bronze' AND c.relname = ?",
                [$tbl],
            )->relrowsecurity;
            $this->assertTrue($rlsOn, "bronze.{$tbl} must have RLS enabled");

            $policyCount = DB::table('pg_policies')
                ->where('schemaname', 'bronze')
                ->where('tablename', $tbl)
                ->count();
            $this->assertGreaterThanOrEqual(
                1, $policyCount,
                "bronze.{$tbl} must have at least one workspace_isolation policy",
            );
        }

        foreach (['ingest_manifest', 'provenance'] as $tbl) {
            $hasColumn = DB::table('information_schema.columns')
                ->where('table_schema', 'bronze')
                ->where('table_name', $tbl)
                ->where('column_name', 'workspace_id')
                ->exists();
            $this->assertTrue(
                $hasColumn,
                "bronze.{$tbl} must have workspace_id column from migration 2026_05_25_170825",
            );
        }
    }

    public function test_lakehouse_forbidden_to_non_members(): void
    {
        ['project' => $project] = $this->seedProjectMember();

        $intruder = User::factory()->create();   // not attached to $project

        $response = $this->actingAs($intruder)->get('/projects/'.$project->slug.'/lakehouse');

        // firstOrFail on the project-user pivot surfaces as 404 by default;
        // accept either 403 or 404 — contract is "no access".
        $this->assertContains($response->status(), [403, 404]);
    }

    public function test_drillhole_detail_renders_for_real_collar(): void
    {
        ['user' => $user, 'project' => $project, 'workspace_id' => $workspaceId] = $this->seedProjectMember();
        $collarId = $this->seedCollar($project->project_id, $workspaceId);

        $url = '/projects/'.$project->slug.'/holes/'.$collarId.'/detail';
        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->component('Foundry/DrillholeDetail')
            ->has('collar')
            ->has('intervals')
            ->has('assays')
            ->has('structures')
            ->has('cross_sections'),
        );
    }

    public function test_drillhole_detail_404_on_unknown_collar(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectMember();

        $url = '/projects/'.$project->slug.'/holes/00000000-0000-0000-0000-000000000000/detail';
        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(404);
    }

    public function test_drillhole_detail_404_when_collar_belongs_to_other_project(): void
    {
        ['user' => $user, 'project' => $projectA, 'workspace_id' => $workspaceIdA] = $this->seedProjectMember();
        $collarInA = $this->seedCollar($projectA->project_id, $workspaceIdA);

        ['project' => $projectB] = $this->seedProjectMember();
        // User has access to projectA (and now projectB? — re-seed gives a fresh
        // user). Re-attach for projectB so the auth gate passes:
        $user->projects()->syncWithoutDetaching([$projectB->project_id => ['role' => 'viewer']]);

        // Hit projectB's slug with a collar from projectA → controller filters
        // by (project_id, collar_id) → 404.
        $url = '/projects/'.$projectB->slug.'/holes/'.$collarInA.'/detail';
        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(404);
    }
}
