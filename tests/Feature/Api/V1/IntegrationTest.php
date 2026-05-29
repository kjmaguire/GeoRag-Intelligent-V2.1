<?php

namespace Tests\Feature\Api\V1;

use App\Jobs\StreamQueryFromFastApi;
use App\Models\Project;
use App\Models\QueryAuditLog;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\Queue;
use Illuminate\Support\Facades\Storage;
use Tests\TestCase;

/**
 * Integration tests for the GeoRAG API — exercises multi-step flows
 * across controllers, jobs, and models.
 */
class IntegrationTest extends TestCase
{
    use RefreshDatabase;

    private User $user;
    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();
        Project::getModel()->setTable('projects');
        $this->user = User::factory()->create();
        $this->project = Project::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);
    }

    // ── Query flow ─────────────────────────────────────────────────────

    public function test_query_dispatches_job_and_creates_audit_log(): void
    {
        Queue::fake();

        // Phase 1 — reserve: audit row is written here; job is NOT dispatched yet.
        $reserve = $this->actingAs($this->user)
            ->postJson('/api/v1/queries', [
                'query'      => 'What are the highest-grade intercepts?',
                'project_id' => $this->project->project_id,
            ]);

        $reserve->assertAccepted()
            ->assertJsonStructure(['query_id', 'channel', 'message']);

        // query_text is encrypted at rest — assert on ids + read back through
        // the model cast to verify the plaintext round-trip.
        $queryId = $reserve->json('query_id');
        $audit = QueryAuditLog::where('query_id', $queryId)->firstOrFail();
        $this->assertSame($this->user->id, $audit->user_id);
        $this->assertSame($this->project->project_id, $audit->project_id);
        $this->assertSame('What are the highest-grade intercepts?', $audit->query_text);

        // Phase 2 — start: job dispatches.
        $this->actingAs($this->user)
            ->postJson("/api/v1/queries/{$queryId}/start")
            ->assertAccepted();

        Queue::assertPushed(StreamQueryFromFastApi::class, 1);
    }

    public function test_query_channel_matches_query_id(): void
    {
        Queue::fake();

        $response = $this->actingAs($this->user)
            ->postJson('/api/v1/queries', [
                'query'      => 'Show me lithology for DH-001.',
                'project_id' => $this->project->project_id,
            ]);

        $response->assertAccepted();
        $this->assertSame(
            "query.{$response->json('query_id')}",
            $response->json('channel')
        );
    }

    public function test_query_returns_401_without_auth(): void
    {
        $this->postJson('/api/v1/queries', [
            'query'      => 'test',
            'project_id' => $this->project->project_id,
        ])->assertUnauthorized();
    }

    // ── Auth flow ──────────────────────────────────────────────────────

    public function test_register_login_logout_cycle(): void
    {
        // Register
        $reg = $this->postJson('/api/v1/auth/register', [
            'name'     => 'Test Geologist',
            'email'    => 'geo@test.com',
            'password' => 'SecureP@ss123',
        ]);
        $reg->assertCreated()
            ->assertJsonStructure(['user' => ['id', 'name', 'email'], 'token']);

        $token = $reg->json('token');

        // Authenticated request with token
        $this->getJson('/api/v1/auth/me', [
            'Authorization' => "Bearer {$token}",
        ])->assertOk()
            ->assertJsonPath('user.email', 'geo@test.com');

        // Logout
        $this->postJson('/api/v1/auth/logout', [], [
            'Authorization' => "Bearer {$token}",
        ])->assertOk();

        // The logout handler calls $token->delete(), which removes the
        // personal_access_tokens row. In production, Octane's
        // FlushAuthenticationState listener clears the auth guard cache
        // between requests, so the next Sanctum lookup hits the DB and
        // fails. Under PHPUnit there's no Octane — the auth guard's
        // resolved user is cached on the kernel-singleton AuthManager
        // and sticks across `getJson()` calls within a single test. We
        // forget guards here so the next request genuinely re-checks
        // the (now deleted) token. Asserting at the DB level would also
        // work, but this preserves the test's end-to-end intent.
        $this->app['auth']->forgetGuards();

        // Token is now revoked — request with the same bearer returns 401.
        $this->getJson('/api/v1/auth/me', [
            'Authorization' => "Bearer {$token}",
        ])->assertUnauthorized();
    }

    public function test_login_with_wrong_password_returns_401(): void
    {
        $this->postJson('/api/v1/auth/login', [
            'email'    => $this->user->email,
            'password' => 'wrong-password',
        ])->assertUnauthorized();
    }

    public function test_duplicate_email_registration_returns_422(): void
    {
        $this->postJson('/api/v1/auth/register', [
            'name'     => 'Duplicate',
            'email'    => $this->user->email,
            'password' => 'SecureP@ss123',
        ])->assertUnprocessable()
            ->assertJsonValidationErrors(['email']);
    }

    public function test_protected_endpoints_return_401_without_token(): void
    {
        $endpoints = [
            ['GET', '/api/v1/projects'],
            ['GET', '/api/v1/auth/me'],
            ['POST', '/api/v1/queries'],
        ];

        foreach ($endpoints as [$method, $uri]) {
            $response = $this->json($method, $uri);
            $this->assertTrue(
                $response->status() === 401,
                "{$method} {$uri} should return 401, got {$response->status()}"
            );
        }
    }

    // ── Export flow ────────────────────────────────────────────────────

    public function test_export_create_list_show_lifecycle(): void
    {
        Queue::fake();

        // Create export
        $create = $this->actingAs($this->user)
            ->postJson("/api/v1/projects/{$this->project->project_id}/exports", [
                'export_type' => 'csv_collars',
            ]);

        $create->assertAccepted()
            ->assertJsonStructure(['data' => ['export_id', 'export_type', 'status']]);

        $exportId = $create->json('data.export_id');

        // List exports
        $list = $this->actingAs($this->user)
            ->getJson("/api/v1/projects/{$this->project->project_id}/exports");

        $list->assertOk();
        $this->assertGreaterThanOrEqual(1, count($list->json('data')));

        // Show export
        $this->actingAs($this->user)
            ->getJson("/api/v1/projects/{$this->project->project_id}/exports/{$exportId}")
            ->assertOk()
            ->assertJsonPath('data.export_id', $exportId);
    }

    // ── Citation resolution ────────────────────────────────────────────

    public function test_citation_resolve_returns_200(): void
    {
        $this->actingAs($this->user)
            ->getJson('/api/v1/citations/resolve?source_chunk_id=collar-test-001&citation_type=DATA')
            ->assertOk();
    }

    public function test_citation_resolve_returns_401_without_auth(): void
    {
        $this->getJson('/api/v1/citations/resolve?source_chunk_id=test')
            ->assertUnauthorized();
    }
}
