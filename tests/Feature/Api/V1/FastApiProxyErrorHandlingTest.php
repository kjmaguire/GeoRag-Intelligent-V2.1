<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Http\Client\Request as HttpRequest;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * LAR-8 (2026-09-29 audit): `->retry(2, 250)` defaults to `throw: true`, so
 * after the last attempt any FastAPI 4xx/5xx threw RequestException. The
 * `! ok() → 502` branch in CoverageDensityController and the `ok() ? json :
 * []` degrade in PublicApiController::interpretations() were dead code:
 * every FastAPI error became an unhandled 500, and 4xx were retried for
 * nothing. Retry now happens only when FastAPI was never reached.
 */
final class FastApiProxyErrorHandlingTest extends TestCase
{
    use RefreshDatabase;

    private Project $project;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.internal_url' => 'http://fastapi.test',
            'services.fastapi.service_key' => 'test-only-service-key-with-at-least-32-bytes',
        ]);

        $this->project = Project::create([
            'project_name' => 'Proxy Errors '.uniqid(),
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);
        DB::table('silver.projects')
            ->where('project_id', $this->project->project_id)
            ->update(['workspace_id' => (string) Str::uuid()]);

        $this->user = User::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);
    }

    private function coverageUrl(): string
    {
        return "/api/v1/projects/{$this->project->project_id}/coverage-density";
    }

    private function densityCalls(): int
    {
        return Http::recorded(fn (HttpRequest $r): bool => str_contains($r->url(), '/coverage/density'))->count();
    }

    public function test_coverage_density_maps_a_fastapi_5xx_to_502_not_500(): void
    {
        Http::fake(['fastapi.test/coverage/density*' => Http::response(['detail' => 'boom'], 500)]);

        $this->actingAs($this->user, 'sanctum')
            ->getJson($this->coverageUrl())
            ->assertStatus(502)
            ->assertJsonPath('message', 'FastAPI coverage density returned HTTP 500');

        $this->assertSame(1, $this->densityCalls(), 'an HTTP answer is not retried');
    }

    public function test_coverage_density_does_not_retry_a_fastapi_4xx(): void
    {
        Http::fake(['fastapi.test/coverage/density*' => Http::response(['detail' => 'bad'], 422)]);

        $this->actingAs($this->user, 'sanctum')
            ->getJson($this->coverageUrl())
            ->assertStatus(502);

        $this->assertSame(1, $this->densityCalls());
    }

    public function test_coverage_density_maps_an_unreachable_fastapi_to_502(): void
    {
        Http::fake(fn () => throw new ConnectionException('Connection refused'));

        $this->actingAs($this->user, 'sanctum')
            ->getJson($this->coverageUrl())
            ->assertStatus(502)
            ->assertJsonPath('message', 'FastAPI coverage density is unreachable.');
    }

    public function test_coverage_density_passes_a_success_through(): void
    {
        Http::fake(['fastapi.test/coverage/density*' => Http::response(['type' => 'FeatureCollection', 'features' => []])]);

        $this->actingAs($this->user, 'sanctum')
            ->getJson($this->coverageUrl())
            ->assertOk()
            ->assertJsonPath('type', 'FeatureCollection');
    }

    public function test_interpretations_degrades_each_failing_list_to_empty(): void
    {
        Http::fake([
            'fastapi.test/v1/interpretation/notes*' => Http::response([['note_id' => 'n1']]),
            'fastapi.test/v1/interpretation/target-zones*' => Http::response(['detail' => 'boom'], 500),
            'fastapi.test/v1/interpretation/section-lines*' => fn () => throw new ConnectionException('Connection refused'),
        ]);

        $this->actingAs($this->user, 'sanctum')
            ->getJson("/api/v1/interpretations/{$this->project->project_id}")
            ->assertOk()
            ->assertJsonPath('notes', [['note_id' => 'n1']])
            ->assertJsonPath('target_zones', [])
            ->assertJsonPath('section_lines', []);
    }
}
