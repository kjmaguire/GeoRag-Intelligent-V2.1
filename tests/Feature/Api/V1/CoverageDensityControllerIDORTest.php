<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Firebase\JWT\JWT;
use Firebase\JWT\Key;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\Request as HttpRequest;
use Illuminate\Log\Events\MessageLogged;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * GET /api/v1/projects/{project}/coverage-density — the tenant boundary.
 *
 * The controller mints a service JWT for FastAPI scoped to the project in the
 * URL. Everything downstream (the SQL, the RLS binding, the Qdrant filter)
 * trusts that token's `project_id` / `workspace_id` claims, so the membership
 * check at the top of show() is the ONLY thing standing between any
 * authenticated user and another tenant's collar density. Until 2026-10-10 the
 * only tests of this route were FastAPI error-mapping cases that attached the
 * caller as an owner (FastApiProxyErrorHandlingTest) and route-shape cases, so
 * removing or inverting hasProjectAccess() broke nothing.
 *
 * What is pinned:
 *   - a non-member gets 404 "Project not found." (not 403: no existence oracle)
 *   - FastAPI is never called for them: no JWT is minted, nothing is sent
 *   - the denial is audited on the `authz_audit` channel (authz.deny)
 *   - membership is decided BEFORE validation, so a non-member cannot probe
 *     the route's parameter handling either
 *   - the positive control: the same URL for a member reaches FastAPI, with a
 *     token scoped to THAT project and workspace. Without it, the 404s above
 *     would also pass for a route that is simply broken.
 */
final class CoverageDensityControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    private const SERVICE_KEY = 'test-only-service-key-with-at-least-32-bytes';

    private User $owner;

    private User $outsider;

    private Project $project;

    private string $workspaceId;

    /** @var list<MessageLogged> */
    private array $denials = [];

    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.internal_url' => 'http://fastapi.test',
            'services.fastapi.service_key' => self::SERVICE_KEY,
        ]);

        $this->workspaceId = (string) Str::uuid();
        $this->project = Project::create([
            'project_name' => 'Coverage IDOR '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);
        DB::table('silver.projects')
            ->where('project_id', $this->project->project_id)
            ->update(['workspace_id' => $this->workspaceId]);

        $this->owner = User::factory()->create();
        $this->owner->projects()->attach($this->project->project_id, ['role' => 'owner']);

        // A user with their OWN project elsewhere: membership of something is
        // not membership of this.
        $this->outsider = User::factory()->create();
        $theirs = Project::create([
            'project_name' => 'Outsider '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);
        $this->outsider->projects()->attach($theirs->project_id, ['role' => 'owner']);

        $this->denials = [];
        Event::listen(MessageLogged::class, function (MessageLogged $event): void {
            if (($event->context['event'] ?? null) === 'authz.deny') {
                $this->denials[] = $event;
            }
        });

        Http::fake(['fastapi.test/*' => Http::response(['type' => 'FeatureCollection', 'features' => []], 200)]);
    }

    private function url(string $projectId, string $query = ''): string
    {
        return "/api/v1/projects/{$projectId}/coverage-density".$query;
    }

    private function fastApiCalls(): int
    {
        return Http::recorded(fn (HttpRequest $r): bool => str_contains($r->url(), 'fastapi.test'))->count();
    }

    public function test_a_non_member_gets_404_and_fastapi_is_never_called(): void
    {
        $this->actingAs($this->outsider, 'sanctum')
            ->getJson($this->url($this->project->project_id))
            ->assertNotFound()
            ->assertExactJson(['message' => 'Project not found.']);

        Http::assertNothingSent();
        $this->assertSame(0, $this->fastApiCalls());
    }

    public function test_the_denial_is_audited_with_who_what_and_why(): void
    {
        $this->actingAs($this->outsider, 'sanctum')
            ->getJson($this->url($this->project->project_id))
            ->assertNotFound();

        $this->assertCount(1, $this->denials, 'exactly one authz.deny event');
        $context = $this->denials[0]->context;
        $this->assertSame((string) $this->outsider->id, $context['actor_user_id']);
        $this->assertSame('project:'.$this->project->project_id, $context['target_resource']);
        $this->assertSame('no_pivot_row', $context['reason']);
        $this->assertSame('show', $context['action']);
        $this->assertSame('api/v1/projects/'.$this->project->project_id.'/coverage-density', $context['path']);
    }

    public function test_a_project_that_does_not_exist_answers_exactly_like_one_the_caller_cannot_see(): void
    {
        $real = $this->actingAs($this->outsider, 'sanctum')
            ->getJson($this->url($this->project->project_id));
        $missing = $this->actingAs($this->outsider, 'sanctum')
            ->getJson($this->url((string) Str::uuid()));

        $missing->assertNotFound();
        $this->assertSame($real->getContent(), $missing->getContent());
        Http::assertNothingSent();
    }

    public function test_membership_is_checked_before_the_query_string_is_validated(): void
    {
        // 422 for a bad `kind` would tell a non-member the project id is
        // real enough to reach validation.
        $this->actingAs($this->outsider, 'sanctum')
            ->getJson($this->url($this->project->project_id, '?kind=not-a-kind&cell_size_m=7'))
            ->assertNotFound()
            ->assertExactJson(['message' => 'Project not found.']);

        Http::assertNothingSent();
    }

    public function test_an_unauthenticated_caller_is_refused_and_fastapi_is_never_called(): void
    {
        $this->getJson($this->url($this->project->project_id))->assertUnauthorized();

        Http::assertNothingSent();
    }

    public function test_a_member_reaches_fastapi_with_a_token_scoped_to_that_project_and_workspace(): void
    {
        $this->actingAs($this->owner, 'sanctum')
            ->getJson($this->url($this->project->project_id, '?kind=collars&cell_size_m=1000'))
            ->assertOk()
            ->assertJsonPath('type', 'FeatureCollection');

        $this->assertSame(1, $this->fastApiCalls());
        $this->assertSame([], $this->denials, 'a member is not a denial');

        Http::assertSent(function (HttpRequest $request): bool {
            $bearer = $request->header('Authorization')[0] ?? '';
            $claims = (array) JWT::decode(Str::after($bearer, 'Bearer '), new Key(self::SERVICE_KEY, 'HS256'));

            return str_starts_with($request->url(), 'http://fastapi.test/coverage/density')
                && ($claims['project_id'] ?? null) === $this->project->project_id
                && ($claims['workspace_id'] ?? null) === $this->workspaceId
                && (string) ($claims['sub'] ?? '') === (string) $this->owner->id
                && ($request->data()['project_id'] ?? null) === $this->project->project_id;
        });
    }
}
