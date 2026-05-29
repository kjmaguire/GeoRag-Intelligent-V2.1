<?php

namespace Tests\Feature\Api\V1;

use App\Jobs\StreamQueryFromFastApi;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Queue;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Phase 3 / Step 3.2 — context envelope plumbing tests.
 *
 * Locks the contract:
 *   - /queries accepts a malformed envelope and returns 422 with a field-
 *     level validation error
 *   - /queries accepts a valid envelope and 202s (envelope is not persisted
 *     here; it flows through /queries/{id}/start)
 *   - /queries/{id}/start forwards the envelope on the StreamQueryFromFastApi
 *     job constructor (validated against the bound queue fake)
 *   - Legacy callers that don't send an envelope still get a 202 and the
 *     job dispatches with null envelope
 */
class QueryControllerContextEnvelopeTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();
        Project::getModel()->setTable('projects');
        $this->user = User::factory()->create();
        $this->actingAs($this->user);
        $this->project = Project::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);
    }

    public function test_store_accepts_valid_envelope_and_returns_202(): void
    {
        Queue::fake();

        $resp = $this->postJson('/api/v1/queries', [
            'query' => 'Recommend infill spacing.',
            'project_id' => $this->project->project_id,
            'context_envelope' => [
                'crs_epsg' => 26913,
                'depth_reference' => 'bgl',
                'data_sources' => ['drill_logs', 'assays'],
                'reporting_code' => 'NI 43-101',
                'decision_to_support' => 'Choose between PLS-22-08 and PLS-22-12 for the next program.',
                'mode' => 'office',
            ],
        ]);

        $resp->assertAccepted()
            ->assertJsonStructure(['query_id', 'channel', 'message']);
        // Store path does NOT dispatch the job (that's /start).
        Queue::assertNothingPushed();
    }

    public function test_store_rejects_invalid_epsg(): void
    {
        $resp = $this->postJson('/api/v1/queries', [
            'query' => 'x',
            'project_id' => $this->project->project_id,
            'context_envelope' => ['crs_epsg' => 999],
        ]);
        $resp->assertStatus(422)->assertJsonValidationErrors(['context_envelope.crs_epsg']);
    }

    public function test_store_rejects_invalid_depth_reference(): void
    {
        $resp = $this->postJson('/api/v1/queries', [
            'query' => 'x',
            'project_id' => $this->project->project_id,
            'context_envelope' => ['depth_reference' => 'BANANA'],
        ]);
        $resp->assertStatus(422)->assertJsonValidationErrors(['context_envelope.depth_reference']);
    }

    public function test_store_rejects_invalid_reporting_code(): void
    {
        $resp = $this->postJson('/api/v1/queries', [
            'query' => 'x',
            'project_id' => $this->project->project_id,
            'context_envelope' => ['reporting_code' => 'SEC'],
        ]);
        $resp->assertStatus(422)->assertJsonValidationErrors(['context_envelope.reporting_code']);
    }

    public function test_store_rejects_invalid_mode(): void
    {
        $resp = $this->postJson('/api/v1/queries', [
            'query' => 'x',
            'project_id' => $this->project->project_id,
            'context_envelope' => ['mode' => 'desk'],
        ]);
        $resp->assertStatus(422)->assertJsonValidationErrors(['context_envelope.mode']);
    }

    public function test_store_rejects_invalid_data_source(): void
    {
        $resp = $this->postJson('/api/v1/queries', [
            'query' => 'x',
            'project_id' => $this->project->project_id,
            'context_envelope' => ['data_sources' => ['drill_logs', 'starlight']],
        ]);
        $resp->assertStatus(422)->assertJsonValidationErrors(['context_envelope.data_sources.1']);
    }

    public function test_start_forwards_envelope_to_job(): void
    {
        Queue::fake();

        // Reserve the query first.
        $reserve = $this->postJson('/api/v1/queries', [
            'query' => 'Should we drill DDH-13?',
            'project_id' => $this->project->project_id,
        ]);
        $reserve->assertAccepted();
        $queryId = $reserve->json('query_id');

        // Start with an envelope — the job must receive it.
        $envelope = [
            'mode' => 'field',
            'crs_epsg' => 26913,
            'data_sources' => ['drill_logs'],
        ];
        $start = $this->postJson("/api/v1/queries/{$queryId}/start", [
            'context_envelope' => $envelope,
        ]);
        $start->assertAccepted();

        Queue::assertPushed(StreamQueryFromFastApi::class, function ($job) use ($queryId, $envelope) {
            $ref = new \ReflectionClass($job);
            $idProp = $ref->getProperty('queryId');
            $idProp->setAccessible(true);
            if ($idProp->getValue($job) !== $queryId) {
                return false;
            }
            $envProp = $ref->getProperty('contextEnvelope');
            $envProp->setAccessible(true);

            return $envProp->getValue($job) === $envelope;
        });
    }

    public function test_start_without_envelope_dispatches_with_null(): void
    {
        Queue::fake();

        $reserve = $this->postJson('/api/v1/queries', [
            'query' => 'Should we drill DDH-13?',
            'project_id' => $this->project->project_id,
        ]);
        $queryId = $reserve->json('query_id');

        $start = $this->postJson("/api/v1/queries/{$queryId}/start");
        $start->assertAccepted();

        Queue::assertPushed(StreamQueryFromFastApi::class, function ($job) {
            $ref = new \ReflectionClass($job);
            $envProp = $ref->getProperty('contextEnvelope');
            $envProp->setAccessible(true);

            return $envProp->getValue($job) === null;
        });
    }

    public function test_start_rejects_malformed_envelope_with_422(): void
    {
        Queue::fake();

        $reserve = $this->postJson('/api/v1/queries', [
            'query' => 'Should we drill DDH-13?',
            'project_id' => $this->project->project_id,
        ]);
        $queryId = $reserve->json('query_id');

        $start = $this->postJson("/api/v1/queries/{$queryId}/start", [
            'context_envelope' => ['crs_epsg' => 99999],
        ]);
        $start->assertStatus(422)->assertJsonStructure(['error', 'message']);
        Queue::assertNothingPushed();
    }
}
