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
 * CC-01 Item 1 Slice 4 — DrillReview Foundry page + decide endpoint.
 *
 * Postgres-only: silver.review_queue lives in the pgsql test DB only
 * (sqlite doesn't have jsonb / uuid / the enum types). Run with
 *   php artisan test -c phpunit.pgsql.xml --filter=DrillReviewControllerTest
 */
final class DrillReviewControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        // Workspace + project member seed (same shape as the §B/S/G suite).
        $this->user = User::factory()->create();
        $this->workspaceId = (string) Str::uuid();
        $slug = 'drill-rev-'.substr($this->workspaceId, 0, 8);

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Drill Rev Workspace', $slug],
        );

        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $this->project->project_id => ['role' => 'owner'],
        ]);
    }

    private function showUrl(): string
    {
        return "/projects/{$this->project->slug}/drill-review";
    }

    private function insertQueueRow(array $overrides = []): string
    {
        $queueId = (string) Str::uuid();
        $defaults = [
            'queue_id' => $queueId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $this->project->project_id,
            'target_table' => 'silver.assays_v2',
            'target_record_kind' => 'sample',
            'bronze_uri' => 's3://georag-bronze/drill-uploads/test/sample.csv',
            'payload' => json_encode(['sample_id' => 'X-001', 'from_depth' => 10.0, 'to_depth' => 11.0]),
            'confidence_per_field' => json_encode([]),
            'confidence_record' => 0.6,
            'parser_version' => 'csv_sample:2.0.0',
            'routing_decision' => 'review_required',
            'routing_reason' => 'unit_ambiguity: 1 flag(s)',
            'outlier_flags' => json_encode([['unit_ambiguity' => ['Au: bare column']]]),
            'lifecycle' => 'pending',
            'created_at' => now(),
            'updated_at' => now(),
        ];
        DB::table('silver.review_queue')->insert(array_merge($defaults, $overrides));

        return $queueId;
    }

    public function test_non_member_user_is_redirected_or_forbidden(): void
    {
        $outsider = User::factory()->create();

        $this->actingAs($outsider)
            ->get($this->showUrl())
            ->assertStatus(404);  // firstOrFail on project_user join
    }

    public function test_show_renders_inertia_page_with_grouped_batches(): void
    {
        $this->insertQueueRow([
            'bronze_uri' => 's3://georag-bronze/drill-uploads/test/batch_a.csv',
        ]);
        $this->insertQueueRow([
            'bronze_uri' => 's3://georag-bronze/drill-uploads/test/batch_a.csv',
            'target_table' => 'silver.lithology',
            'target_record_kind' => 'lithology',
        ]);
        $this->insertQueueRow([
            'bronze_uri' => 's3://georag-bronze/drill-uploads/test/batch_b.csv',
        ]);

        $response = $this->actingAs($this->user)->get($this->showUrl());

        $response->assertOk();
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->component('Foundry/DrillReview')
            ->where('project.slug', $this->project->slug)
            ->has('batches', 2)
            ->where('counters.pending', 3)
            ->where('counters.in_review', 0)
            ->where('counters.decided', 0),
        );
    }

    public function test_show_does_not_surface_non_drill_target_tables(): void
    {
        $this->insertQueueRow();  // silver.assays_v2 — should show
        $this->insertQueueRow([
            'target_table' => 'silver.unrelated_review_target',
        ]);

        $response = $this->actingAs($this->user)->get($this->showUrl());
        $response->assertOk();
        $response->assertInertia(fn (AssertableInertia $page) => $page
            // Only the drill batch should remain.
            ->where('counters.pending', 1),
        );
    }

    public function test_decide_records_decision_and_advances_lifecycle(): void
    {
        $queueId = $this->insertQueueRow();

        $this->actingAs($this->user)
            ->post(
                "/projects/{$this->project->slug}/drill-review/{$queueId}/decide",
                [
                    'decision_kind' => 'approve_as_parsed',
                    'decision_rationale' => 'Reviewed, clean.',
                ],
            )
            ->assertRedirect();

        $row = DB::table('silver.review_queue')->where('queue_id', $queueId)->first();
        $this->assertSame('decided', $row->lifecycle);
        $this->assertSame('approve_as_parsed', $row->decision_kind);
        $this->assertSame($this->user->id, (int) $row->decided_by_user_id);
        $this->assertNotNull($row->decided_at);
        // Non-corrections decisions MUST have null payload (CHECK constraint).
        $this->assertNull($row->decision_payload);
    }

    public function test_decide_rejects_invalid_decision_kind(): void
    {
        $queueId = $this->insertQueueRow();

        $this->actingAs($this->user)
            ->post(
                "/projects/{$this->project->slug}/drill-review/{$queueId}/decide",
                ['decision_kind' => 'invalid_value'],
            )
            ->assertSessionHasErrors(['decision_kind']);
    }

    public function test_decide_refuses_already_decided_row(): void
    {
        $queueId = $this->insertQueueRow([
            'lifecycle' => 'decided',
            'decision_kind' => 'approve_as_parsed',
            'decided_at' => now(),
            'decided_by_user_id' => $this->user->id,
        ]);

        $response = $this->actingAs($this->user)
            ->post(
                "/projects/{$this->project->slug}/drill-review/{$queueId}/decide",
                ['decision_kind' => 'reject'],
            );

        $response->assertSessionHasErrors(['lifecycle']);

        // Row state must not have changed.
        $row = DB::table('silver.review_queue')->where('queue_id', $queueId)->first();
        $this->assertSame('approve_as_parsed', $row->decision_kind);
    }

    public function test_decide_with_corrections_persists_payload(): void
    {
        $queueId = $this->insertQueueRow();

        $this->actingAs($this->user)
            ->post(
                "/projects/{$this->project->slug}/drill-review/{$queueId}/decide",
                [
                    'decision_kind' => 'approve_with_corrections',
                    'decision_payload' => ['from_depth' => 10.5],
                    'decision_rationale' => 'Caller mis-entered from_depth.',
                ],
            )
            ->assertRedirect();

        $row = DB::table('silver.review_queue')->where('queue_id', $queueId)->first();
        $this->assertSame('approve_with_corrections', $row->decision_kind);
        $payload = json_decode((string) $row->decision_payload, true);
        $this->assertSame(['from_depth' => 10.5], $payload);
    }
}
