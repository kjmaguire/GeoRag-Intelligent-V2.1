<?php

declare(strict_types=1);

namespace Tests\Feature\Ingestion;

use App\Models\Project;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Http\Client\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Storage;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * `ingest:reingest-project` is destructive: it deletes a project's Qdrant
 * points and silver.reports rows, then re-triggers ingestion. It used to do the
 * deleting FIRST and look for what the re-triggering needs afterwards, and to
 * carry on after a failed Qdrant delete (the helper returned an error as a
 * string and the caller printed it). Either left a project with no corpus.
 */
final class ReingestProjectOrderingTest extends TestCase
{
    use RefreshDatabase;

    private const SERVICE_KEY = 'reingest-test-service-key-at-least-32-bytes';

    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.service_key' => self::SERVICE_KEY,
            'services.fastapi.internal_url' => 'http://fastapi.test',
        ]);

        $this->project = Project::factory()->create();
        foreach (['one', 'two'] as $name) {
            DB::table('silver.reports')->insert([
                'report_id' => (string) Str::uuid(),
                'project_id' => $this->project->project_id,
                'title' => "Report {$name}",
                'created_at' => now(),
                'updated_at' => now(),
            ]);
        }

        // Two PDFs sitting in bronze for the project.
        Storage::fake('s3');
        foreach (['one', 'two'] as $name) {
            Storage::disk('s3')->put("reports/{$this->project->project_id}/{$name}.pdf", "%PDF-1.4 {$name}");
        }
    }

    private function reports(): int
    {
        return DB::table('silver.reports')->where('project_id', $this->project->project_id)->count();
    }

    /** @return array<string, mixed> */
    private function commandArguments(array $extra = []): array
    {
        return array_merge(['projectId' => $this->project->project_id, '--throttle-ms' => 0], $extra);
    }

    private function qdrantOk(): array
    {
        return ['qdrant:6333/*' => Http::response(['status' => 'ok', 'result' => ['operation_id' => 7, 'status' => 'completed']])];
    }

    private function fastapiOk(): array
    {
        return ['fastapi.test/*' => Http::response(['workflow_run_id' => 'wf-1'])];
    }

    public function test_a_missing_service_key_deletes_nothing(): void
    {
        config(['services.fastapi.service_key' => null]);
        Http::fake($this->qdrantOk() + $this->fastapiOk());

        $this->artisan('ingest:reingest-project', $this->commandArguments())
            ->expectsOutputToContain('FASTAPI_SERVICE_KEY not configured')
            ->assertFailed();

        $this->assertSame(2, $this->reports(), 'the reports are still there');
        Http::assertNothingSent();
    }

    public function test_a_key_the_jwt_minter_refuses_deletes_nothing(): void
    {
        // FastApiJwtMinter rejects a secret under 32 bytes. That used to be
        // discovered after the delete.
        config(['services.fastapi.service_key' => 'too-short']);
        Http::fake($this->qdrantOk() + $this->fastapiOk());

        $this->artisan('ingest:reingest-project', $this->commandArguments())
            ->expectsOutputToContain('Cannot mint the FastAPI service token')
            ->assertFailed();

        $this->assertSame(2, $this->reports());
        Http::assertNothingSent();
    }

    public function test_a_failed_qdrant_delete_stops_before_the_reports_are_deleted(): void
    {
        Http::fake([
            'qdrant:6333/*' => Http::response(['status' => ['error' => 'collection is locked']], 500),
        ] + $this->fastapiOk());

        $this->artisan('ingest:reingest-project', $this->commandArguments())
            ->expectsOutputToContain('Qdrant delete failed: HTTP 500')
            ->expectsOutputToContain('nothing was deleted from Postgres')
            ->assertFailed();

        $this->assertSame(2, $this->reports());
        Http::assertSentCount(1);
        Http::assertSent(fn (Request $r): bool => str_contains($r->url(), '/points/delete'));
    }

    public function test_an_unreachable_qdrant_stops_before_the_reports_are_deleted(): void
    {
        Http::fake([
            'qdrant:6333/*' => fn () => throw new ConnectionException('Connection refused'),
        ] + $this->fastapiOk());

        $this->artisan('ingest:reingest-project', $this->commandArguments())
            ->expectsOutputToContain('Qdrant delete failed: Connection refused')
            ->assertFailed();

        $this->assertSame(2, $this->reports());
    }

    public function test_a_good_run_deletes_then_triggers_each_pdf_with_its_own_token(): void
    {
        Http::fake($this->qdrantOk() + $this->fastapiOk());

        // The throttle (1.1 s, the one second this test costs) puts the second
        // trigger in a later second than the first. A token's iat is in whole
        // seconds, so the two differ only if each trigger mints its own -- one
        // minted up front would be sent twice, and expire after 60 s.
        $this->artisan('ingest:reingest-project', $this->commandArguments(['--throttle-ms' => 1100]))
            ->expectsOutputToContain('triggered=2 failed=0')
            ->assertSuccessful();

        $this->assertSame(0, $this->reports());

        $triggers = [];
        Http::assertSent(function (Request $r) use (&$triggers): bool {
            if (str_contains($r->url(), '/internal/v1/shadow/ingest_pdf/trigger')) {
                $triggers[] = $r;
            }

            return true;
        });
        $this->assertCount(2, $triggers);
        foreach ($triggers as $trigger) {
            $this->assertStringStartsWith('Bearer ', $trigger->header('Authorization')[0]);
            $this->assertSame(self::SERVICE_KEY, $trigger->header('X-Service-Key')[0]);
        }
        $this->assertNotSame(
            $triggers[0]->header('Authorization')[0],
            $triggers[1]->header('Authorization')[0],
            'each trigger carries a freshly minted token (they live 60 s)',
        );
    }

    public function test_a_dry_run_needs_no_key_and_changes_nothing(): void
    {
        config(['services.fastapi.service_key' => null]);
        Http::fake($this->qdrantOk() + $this->fastapiOk());

        $this->artisan('ingest:reingest-project', $this->commandArguments(['--dry-run' => true]))
            ->expectsOutputToContain('--dry-run set')
            ->assertSuccessful();

        $this->assertSame(2, $this->reports());
        Http::assertNothingSent();
    }

    public function test_missing_only_never_touches_qdrant_or_the_reports(): void
    {
        Http::fake($this->qdrantOk() + $this->fastapiOk());

        $this->artisan('ingest:reingest-project', $this->commandArguments(['--missing-only' => true]))
            ->assertSuccessful();

        $this->assertSame(2, $this->reports());
        Http::assertNotSent(fn (Request $r): bool => str_contains($r->url(), '/points/delete'));
    }
}
