<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Http\Client\Request as ClientRequest;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * GET /api/v1/answer-runs/{id}/trust-summary (TrustController).
 *
 * The project used to come from `?project_id=` and was authorised as given,
 * so a member of project A could name A together with a run id belonging to
 * sibling project B in the same workspace and read B's trust summary. The
 * project is now looked up from silver.answer_runs. Upstream 401/403/419 are
 * also mapped to a neutral 502: passed through, the SPA's global fetch
 * wrapper treats them as an expired session and logs the user out.
 *
 * Postgres-only: silver.answer_runs is a raw-SQL migration.
 */
final class TrustControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $projectA;

    private Project $projectB;

    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        config(['services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long']);

        $this->workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())',
            [$this->workspaceId, 'Trust Test', 'trust-'.substr($this->workspaceId, 0, 8)],
        );

        $this->projectA = $this->projectInWorkspace();
        $this->projectB = $this->projectInWorkspace();

        $this->user = User::factory()->create();
        $this->user->projects()->syncWithoutDetaching([$this->projectA->project_id => ['role' => 'viewer']]);
    }

    private function projectInWorkspace(): Project
    {
        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $project->project_id],
        );

        return $project;
    }

    private function answerRunIn(Project $project): string
    {
        $id = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.answer_runs
                (answer_run_id, workspace_id, project_id, query_text, query_class,
                 workspace_data_version_at_query)
             VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, ?)',
            [$id, $this->workspaceId, $project->project_id, 'q', 'factual', 1],
        );

        return $id;
    }

    private function url(string $runId, string $query = ''): string
    {
        return "/api/v1/answer-runs/{$runId}/trust-summary".$query;
    }

    /**
     * @return array<string, mixed>
     */
    private function jwtClaims(ClientRequest $request): array
    {
        $token = substr($request->header('Authorization')[0], strlen('Bearer '));
        $payload = explode('.', $token)[1];

        return json_decode(base64_decode(strtr($payload, '-_', '+/')), true);
    }

    public function test_a_sibling_projects_run_is_404_even_when_the_callers_own_project_id_is_named(): void
    {
        Http::fake();
        $foreignRun = $this->answerRunIn($this->projectB);

        $this->actingAs($this->user, 'sanctum')
            ->getJson($this->url($foreignRun, '?project_id='.$this->projectA->project_id))
            ->assertNotFound();

        Http::assertNothingSent();
    }

    public function test_the_callers_own_run_is_served_without_any_project_id_in_the_query(): void
    {
        Http::fake(['*/trust-summary' => Http::response(['sections' => []], 200)]);
        $run = $this->answerRunIn($this->projectA);

        $this->actingAs($this->user, 'sanctum')
            ->getJson($this->url($run))
            ->assertOk()
            ->assertJsonPath('sections', []);

        Http::assertSent(fn (ClientRequest $r) => $this->jwtClaims($r)['project_id'] === $this->projectA->project_id
            && $this->jwtClaims($r)['workspace_id'] === $this->workspaceId);
    }

    public function test_the_jwt_carries_the_runs_project_not_the_query_strings(): void
    {
        Http::fake(['*/trust-summary' => Http::response(['sections' => []], 200)]);
        $this->user->projects()->syncWithoutDetaching([$this->projectB->project_id => ['role' => 'viewer']]);
        $runInB = $this->answerRunIn($this->projectB);

        $this->actingAs($this->user, 'sanctum')
            ->getJson($this->url($runInB, '?project_id='.$this->projectA->project_id))
            ->assertOk();

        Http::assertSent(fn (ClientRequest $r) => $this->jwtClaims($r)['project_id'] === $this->projectB->project_id);
    }

    public function test_unknown_and_malformed_run_ids_are_404(): void
    {
        Http::fake();

        $this->actingAs($this->user, 'sanctum')->getJson($this->url((string) Str::uuid()))->assertNotFound();
        $this->actingAs($this->user, 'sanctum')->getJson($this->url('not-a-uuid'))->assertNotFound();

        Http::assertNothingSent();
    }

    /**
     * @return array<string, array{0: int}>
     */
    public static function sessionLookingStatuses(): array
    {
        return ['401' => [401], '403' => [403], '419' => [419]];
    }

    #[DataProvider('sessionLookingStatuses')]
    public function test_upstream_auth_statuses_become_a_neutral_502(int $upstream): void
    {
        Http::fake(['*/trust-summary' => Http::response(['detail' => 'service key rejected'], $upstream)]);
        $run = $this->answerRunIn($this->projectA);

        $this->actingAs($this->user, 'sanctum')
            ->getJson($this->url($run))
            ->assertStatus(502)
            ->assertJsonPath('error', 'upstream_unavailable')
            ->assertJsonMissingPath('body')
            ->assertJsonMissingPath('status');
    }

    public function test_other_upstream_statuses_still_pass_through(): void
    {
        Http::fake(['*/trust-summary' => Http::response(['detail' => 'nope'], 404)]);
        $run = $this->answerRunIn($this->projectA);

        $this->actingAs($this->user, 'sanctum')->getJson($this->url($run))->assertStatus(404);
    }

    public function test_a_non_2xx_upstream_body_is_not_forwarded_but_is_logged(): void
    {
        Http::fake(['*/trust-summary' => Http::response(
            ['detail' => 'asyncpg.exceptions.ConnectionDoesNotExistError at georag-postgresql:5432'],
            500,
        )]);
        Log::spy();
        $run = $this->answerRunIn($this->projectA);

        $response = $this->actingAs($this->user, 'sanctum')
            ->getJson($this->url($run))
            ->assertStatus(500)
            ->assertExactJson(['error' => 'upstream_error', 'status' => 500]);

        $this->assertStringNotContainsString('asyncpg', $response->getContent());
        Log::shouldHaveReceived('warning')->withArgs(
            fn (string $message, array $context): bool => $context['status'] === 500
                && str_contains((string) $context['body'], 'asyncpg'),
        )->once();
    }

    public function test_an_unreachable_upstream_is_a_502_without_internal_detail(): void
    {
        Http::fake(function (): void {
            throw new ConnectionException('cURL error 7: connect to fastapi.internal:8000');
        });
        $run = $this->answerRunIn($this->projectA);

        $this->actingAs($this->user, 'sanctum')
            ->getJson($this->url($run))
            ->assertStatus(502)
            ->assertJsonMissingPath('reason');
    }
}
