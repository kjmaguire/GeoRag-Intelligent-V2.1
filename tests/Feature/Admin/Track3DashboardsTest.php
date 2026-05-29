<?php

declare(strict_types=1);

namespace Tests\Feature\Admin;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Doc-phase 157 — Inertia route-smoke tests for the 4 Track-3 admin
 * surfaces (Eval Dashboard, Decision History, Support Cockpit,
 * Hypothesis Workspace).
 *
 * Each test asserts the standard auth flow + Inertia component name
 * + presence of the structural prop keys the React pages depend on.
 * Does NOT assert on data shape inside (that's covered by the
 * controller-side reflection smoke tests).
 *
 * Gated on the postgres test connection (the 4 dashboard controllers
 * all read raw SQL against silver/ops/audit/eval schemas).
 */
class Track3DashboardsTest extends TestCase
{
    // RequiresPostgres gates on the pgsql config so the doc-phase 133
    // platform_ops migration's PG-specific `?::uuid` casts work. Under
    // sqlite the trait skips before RefreshDatabase fires.
    use RefreshDatabase;
    use RequiresPostgres;

    // ── Eval Dashboard ────────────────────────────────────────────────
    public function test_eval_dashboard_guest_is_redirected(): void
    {
        $this->get('/admin/eval-dashboard')->assertRedirect('/login');
    }

    public function test_eval_dashboard_non_admin_is_forbidden(): void
    {
        $user = User::factory()->create(['is_admin' => false]);
        $this->actingAs($user, 'sanctum');
        $this->get('/admin/eval-dashboard')->assertForbidden();
    }

    public function test_eval_dashboard_admin_renders_with_expected_props(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');
        $response = $this->get('/admin/eval-dashboard');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->component('Admin/EvalDashboard')
            ->has('kpis')
            ->has('questions_by_set')
            ->has('questions_by_difficulty')
            ->has('ontology_progress')
            ->has('recent_runs')
            // Doc-phase 171 — §04i failure-layer breakdown panel
            ->has('failure_layer_breakdown')
        );
    }

    /**
     * Doc-phase 171 — failure_layer_breakdown returns the full 8-bucket
     * canonical layer set even on an empty database. Operators see all
     * §04i layers + 2 infra rows every render, with zero-counts
     * gracefully muted in the UI.
     */
    public function test_eval_dashboard_failure_layer_breakdown_returns_canonical_buckets(): void
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin, 'sanctum');
        $response = $this->get('/admin/eval-dashboard');
        $response->assertOk();
        $response->assertInertia(fn ($page) => $page
            ->has('failure_layer_breakdown', 8)
            ->where('failure_layer_breakdown.0.failure_layer', '6_refusal')
            ->where('failure_layer_breakdown.1.failure_layer', '2_citation_presence')
            ->where('failure_layer_breakdown.2.failure_layer', '5_chunk_provenance')
            ->where('failure_layer_breakdown.3.failure_layer', '4_entity_resolution')
            ->where('failure_layer_breakdown.4.failure_layer', '3_numeric_claims')
            ->where('failure_layer_breakdown.5.failure_layer', '1_retrieval_quality')
            ->where('failure_layer_breakdown.6.failure_layer', 'refusal')
            ->where('failure_layer_breakdown.7.failure_layer', 'evaluator_not_ready')
            // Every bucket has the expected shape, even with zero failures
            ->has('failure_layer_breakdown.0.fail_count')
            ->has('failure_layer_breakdown.0.last_failed_at')
        );
    }

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
            ->has('valid_decision_types')
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
            ->has('valid_categories')
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
            ->where('filters.status', 'investigating')
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
            ->has('valid_evidence_roles')
        );
    }
}
