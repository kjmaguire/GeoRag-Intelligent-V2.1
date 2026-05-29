<?php

declare(strict_types=1);

namespace Tests\Feature\Admin;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Doc-phase 183 — Inertia route-smoke tests for the Cluster Ingest
 * admin dashboard.
 *
 * Mirrors the Track3DashboardsTest pattern: auth flow + Inertia
 * component name + presence of structural prop keys.
 */
class ClusterIngestTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    public function test_cluster_ingest_guest_is_redirected(): void
    {
        $this->get('/admin/cluster-ingest')->assertRedirect('/login');
    }

    public function test_cluster_ingest_non_admin_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');
        $this->get('/admin/cluster-ingest')->assertForbidden();
    }

    public function test_cluster_ingest_admin_renders_with_expected_props(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');
        $response = $this->get('/admin/cluster-ingest');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/ClusterIngest')
            ->has('kpis')
            ->has('kpis.total_ingest_runs')
            ->has('kpis.total_collars')
            ->has('kpis.passages_embedded')
            ->has('kpis.passages_pending_embed')
            ->has('recent_runs')
            ->has('top_clusters')
            ->has('per_project')
        );
    }

    public function test_cluster_ingest_kpis_are_integers(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');
        $response = $this->get('/admin/cluster-ingest');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->where('kpis.total_ingest_runs', fn ($v) => is_int($v))
            ->where('kpis.total_files_indexed', fn ($v) => is_int($v))
            ->where('kpis.total_collars', fn ($v) => is_int($v))
            ->where('kpis.passages_embedded', fn ($v) => is_int($v))
        );
    }
}
