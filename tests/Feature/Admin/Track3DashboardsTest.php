<?php

declare(strict_types=1);

namespace Tests\Feature\Admin;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Doc-phase 157 — Inertia route-smoke tests for the Track-3 admin
 * surfaces (Decision History, Support Cockpit, Hypothesis Workspace).
 *
 * Each test asserts the standard auth flow + Inertia component name
 * + presence of the structural prop keys the React pages depend on.
 * Does NOT assert on data shape inside (that's covered by the
 * controller-side reflection smoke tests).
 *
 * Gated on the postgres test connection because the dashboard controllers
 * read raw SQL against silver/ops/audit schemas.
 */
class Track3DashboardsTest extends TestCase
{
    // RequiresPostgres gates on the pgsql config so the doc-phase 133
    // platform_ops migration's PG-specific `?::uuid` casts work. Under
    // sqlite the trait skips before RefreshDatabase fires.
    use RefreshDatabase;
    use RequiresPostgres;

    // ── Decision History ──────────────────────────────────────────────
    public function test_decision_history_guest_is_redirected(): void
    {
        $this->get('/admin/decision-history')->assertRedirect('/login');
    }

    public function test_decision_history_non_admin_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');
        $this->get('/admin/decision-history')->assertForbidden();
    }

    public function test_decision_history_admin_renders_with_expected_props(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');
        $response = $this->get('/admin/decision-history');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/DecisionHistory')
            ->has('kpis')
            ->has('by_decision_type')
            ->has('by_human_decision')
            ->has('recent_decisions')
            ->has('recent_audit_anchors')
            ->has('valid_decision_types'),
        );
    }

    // ── Support Cockpit ───────────────────────────────────────────────
    public function test_support_cockpit_guest_is_redirected(): void
    {
        $this->get('/admin/support-cockpit')->assertRedirect('/login');
    }

    public function test_support_cockpit_non_admin_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');
        $this->get('/admin/support-cockpit')->assertForbidden();
    }

    public function test_support_cockpit_admin_renders_with_expected_props(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');
        $response = $this->get('/admin/support-cockpit');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/SupportCockpit')
            ->has('kpis')
            ->has('by_status')
            ->has('by_severity')
            ->has('by_category')
            ->has('recent_tickets')
            ->has('recent_accesses')
            ->has('recent_replays')
            ->has('valid_statuses')
            ->has('valid_severities')
            ->has('valid_categories'),
        );
    }

    public function test_support_cockpit_status_filter_passes_through(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');
        $response = $this->get('/admin/support-cockpit?status=investigating');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/SupportCockpit')
            ->where('filters.status', 'investigating'),
        );
    }

    // ── Hypothesis Workspace ──────────────────────────────────────────
    public function test_hypothesis_workspace_guest_is_redirected(): void
    {
        $this->get('/admin/hypothesis-workspace')->assertRedirect('/login');
    }

    public function test_hypothesis_workspace_non_admin_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');
        $this->get('/admin/hypothesis-workspace')->assertForbidden();
    }

    public function test_hypothesis_workspace_admin_renders_with_expected_props(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');
        $response = $this->get('/admin/hypothesis-workspace');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/HypothesisWorkspace')
            ->has('kpis')
            ->has('by_review_status')
            ->has('by_confidence_method')
            ->has('by_evidence_role')
            ->has('recent_hypotheses')
            ->has('recent_evidence_links')
            ->has('valid_review_statuses')
            ->has('valid_evidence_roles'),
        );
    }
}
