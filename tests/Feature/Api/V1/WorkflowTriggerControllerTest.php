<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use GuzzleHttp\Exception\ConnectException;
use GuzzleHttp\Psr7\Request as PsrRequest;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Http\Client\Request as HttpRequest;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Sleep;
use Tests\TestCase;

/**
 * HAT-13 (2026-09-29) — the product triggers for the Hatchet workflows that
 * were reachable only from the Hatchet UI.
 *
 * FastAPI is faked with Http::fake. These tests pin four things: who may
 * trigger (auth, admin, project and workspace membership), that the workspace
 * sent to FastAPI is the one the user was authorised for, that identity fields
 * come from the authenticated user rather than the body, and how a FastAPI
 * refusal maps to the Laravel response.
 */
final class WorkflowTriggerControllerTest extends TestCase
{
    use RefreshDatabase;

    private const BASE = 'http://fastapi.test/internal/v1/workflows/';

    private const EXPORTS_BUCKET = 'georag-exports-123456789012';

    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long',
            'services.fastapi.internal_url' => 'http://fastapi.test',
            // What deploy/aws/terraform/config.tf sets as AWS_BUCKET_EXPORTS.
            'filesystems.disks.s3-exports.bucket' => self::EXPORTS_BUCKET,
        ]);
    }

    private function fakeAccepted(string $runId = 'run-abc'): void
    {
        Http::fake([
            self::BASE.'*' => fn (HttpRequest $request) => Http::response([
                'workflow' => basename(dirname($request->url())),
                'workflow_run_id' => $runId,
                'workspace_id' => $request['workspace_id'],
            ], 202),
        ]);
    }

    /**
     * @return array{0: User, 1: Project}
     */
    private function member(bool $admin = false): array
    {
        $project = Project::factory()->create();
        $user = ($admin ? User::factory()->admin() : User::factory())->create();
        $user->projects()->attach($project->project_id, ['role' => 'owner']);

        return [$user, $project];
    }

    // ── Project-scoped: generate_report, score_targets ──────────────────

    public function test_project_trigger_requires_authentication(): void
    {
        $project = Project::factory()->create();

        $this->postJson("/api/v1/projects/{$project->project_id}/workflows/generate_report", [
            'report_type' => 'ingestion_quality',
        ])->assertUnauthorized();
    }

    public function test_non_member_gets_404_and_nothing_is_dispatched(): void
    {
        Http::fake();
        [, $project] = $this->member();

        $this->actingAs(User::factory()->admin()->create())
            ->postJson("/api/v1/projects/{$project->project_id}/workflows/generate_report", [
                'report_type' => 'ingestion_quality',
            ])
            ->assertNotFound()
            ->assertJsonPath('message', 'Project not found.');

        Http::assertNothingSent();
    }

    public function test_member_generates_a_report_scoped_to_the_projects_workspace(): void
    {
        $this->fakeAccepted('run-report');
        [$user, $project] = $this->member();

        $this->actingAs($user)
            ->postJson("/api/v1/projects/{$project->project_id}/workflows/generate_report", [
                'report_type' => 'what_changed',
                'report_window_start' => '2026-09-01',
                'report_window_end' => '2026-09-28',
                // Ignored: identity comes from the session, not the body.
                'requested_by_user_id' => 999999,
            ])
            ->assertStatus(202)
            ->assertJson(['workflow' => 'generate_report', 'workflow_run_id' => 'run-report']);

        Http::assertSent(function (HttpRequest $request) use ($user, $project): bool {
            $input = $request['input'];

            return $request->url() === self::BASE.'generate_report/trigger'
                && $request->hasHeader('X-Service-Key', 'test-service-key-must-be-at-least-32-bytes-long')
                && str_starts_with($request->header('Authorization')[0] ?? '', 'Bearer ')
                && $request['workspace_id'] === (string) $project->workspace_id
                && $request['requested_by'] === 'web:'.$user->email
                && $input['workspace_id'] === (string) $project->workspace_id
                && $input['project_id'] === (string) $project->project_id
                && $input['requested_by_user_id'] === $user->getKey()
                && $input['report_type'] === 'what_changed'
                && str_starts_with((string) $input['report_window_start_iso'], '2026-09-01T00:00:00')
                && is_string($input['export_request_id']) && strlen($input['export_request_id']) === 36;
        });
    }

    public function test_unknown_report_type_is_422_and_not_dispatched(): void
    {
        Http::fake();
        [$user, $project] = $this->member();

        $this->actingAs($user)
            ->postJson("/api/v1/projects/{$project->project_id}/workflows/generate_report", [
                'report_type' => 'anything',
            ])
            ->assertUnprocessable()
            ->assertJsonValidationErrors('report_type');

        Http::assertNothingSent();
    }

    public function test_score_targets_needs_candidate_zones_and_sends_them(): void
    {
        $this->fakeAccepted('run-score');
        [$user, $project] = $this->member();
        $url = "/api/v1/projects/{$project->project_id}/workflows/score_targets";
        $aoi = 'POLYGON((0 0,1 0,1 1,0 1,0 0))';

        $this->actingAs($user)->postJson($url, ['aoi_geom_wkt' => $aoi])
            ->assertUnprocessable()
            ->assertJsonValidationErrors('candidate_zone_wkts');
        Http::assertNothingSent();

        $this->actingAs($user)
            ->postJson($url, ['aoi_geom_wkt' => $aoi, 'candidate_zone_wkts' => [$aoi]])
            ->assertStatus(202);

        Http::assertSent(fn (HttpRequest $request): bool => $request->url() === self::BASE.'score_targets/trigger'
            && $request['input']['extra_candidate_zone_wkts'] === [$aoi]
            && $request['input']['scoring_kind'] === 'weighted'
            && $request['input']['requested_by_user_id'] === $user->getKey());
    }

    public function test_a_workflow_outside_the_route_allow_list_does_not_match(): void
    {
        Http::fake();
        [$user, $project] = $this->member(admin: true);

        $this->actingAs($user)
            ->postJson("/api/v1/projects/{$project->project_id}/workflows/ingest_pdf")
            ->assertNotFound();
        $this->actingAs($user)
            ->postJson("/api/v1/projects/{$project->project_id}/workflows/field_outcome_learning")
            ->assertNotFound();

        Http::assertNothingSent();
    }

    public function test_identical_request_within_the_cooldown_is_429_with_the_first_run_id(): void
    {
        $this->fakeAccepted('run-first');
        [$user, $project] = $this->member();
        $url = "/api/v1/projects/{$project->project_id}/workflows/generate_report";

        $this->actingAs($user)->postJson($url, ['report_type' => 'ingestion_quality'])->assertStatus(202);
        $this->actingAs($user)->postJson($url, ['report_type' => 'ingestion_quality'])
            ->assertStatus(429)
            ->assertJson(['error' => 'workflow_recently_triggered', 'workflow_run_id' => 'run-first']);
        // A different request is not a double click.
        $this->actingAs($user)->postJson($url, ['report_type' => 'data_room_package'])->assertStatus(202);

        Http::assertSentCount(2);
    }

    public function test_fastapi_404_passes_through_and_releases_the_cooldown(): void
    {
        Http::fake([
            self::BASE.'*' => Http::sequence()
                ->push(['detail' => 'project not found in this workspace'], 404)
                ->push(['workflow' => 'generate_report', 'workflow_run_id' => 'run-retry', 'workspace_id' => null], 202),
        ]);
        [$user, $project] = $this->member();
        $url = "/api/v1/projects/{$project->project_id}/workflows/generate_report";

        $this->actingAs($user)->postJson($url, ['report_type' => 'ingestion_quality'])
            ->assertNotFound()
            ->assertJsonPath('error', 'trigger_failed');
        $this->actingAs($user)->postJson($url, ['report_type' => 'ingestion_quality'])
            ->assertStatus(202)
            ->assertJsonPath('workflow_run_id', 'run-retry');
    }

    public function test_fastapi_unreachable_is_502(): void
    {
        Http::fake(fn () => throw new ConnectionException('Connection refused'));
        [$user, $project] = $this->member();

        $this->actingAs($user)
            ->postJson("/api/v1/projects/{$project->project_id}/workflows/generate_report", [
                'report_type' => 'ingestion_quality',
            ])
            ->assertStatus(502)
            ->assertJsonPath('error', 'trigger_failed');
    }

    public function test_a_non_hatchet_exception_releases_the_cooldown(): void
    {
        // The JWT minter throws a plain RuntimeException for a signing key
        // shorter than 32 bytes. trigger() does not translate it, so before
        // the fix only HatchetWorkflowTriggerException released the 60 s claim
        // and the user's immediate retry was answered 429 for a run that never
        // went anywhere.
        $this->fakeAccepted('run-after-crash');
        [$user, $project] = $this->member();
        $url = "/api/v1/projects/{$project->project_id}/workflows/generate_report";

        config(['services.fastapi.service_key' => 'too-short']);
        $this->actingAs($user)->postJson($url, ['report_type' => 'ingestion_quality'])->assertStatus(500);

        config(['services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long']);
        $this->actingAs($user)->postJson($url, ['report_type' => 'ingestion_quality'])
            ->assertStatus(202)
            ->assertJsonPath('workflow_run_id', 'run-after-crash');
    }

    public function test_an_unreachable_fastapi_does_not_disclose_the_upstream_detail(): void
    {
        Http::fake(fn () => throw new ConnectionException('cURL error 7: Failed to connect to fastapi.georag.internal port 8000'));
        [$user, $project] = $this->member();

        $response = $this->actingAs($user)
            ->postJson("/api/v1/projects/{$project->project_id}/workflows/generate_report", [
                'report_type' => 'ingestion_quality',
            ])
            ->assertStatus(502)
            ->assertJsonPath('error', 'trigger_failed');

        $this->assertStringNotContainsString('georag.internal', (string) $response->getContent());
    }

    // ── Finding 8: what a transport failure means ───────────────────────

    private function transportFailure(int $errno): ConnectionException
    {
        $guzzle = new ConnectException(
            "cURL error {$errno}: simulated",
            new PsrRequest('POST', self::BASE.'generate_report/trigger'),
            null,
            ['errno' => $errno],
        );

        return new ConnectionException($guzzle->getMessage(), 0, $guzzle);
    }

    public function test_a_connect_failure_is_retried_and_a_final_one_releases_the_cooldown(): void
    {
        Sleep::fake();
        $attempts = 0;
        Http::fake(function () use (&$attempts) {
            $attempts++;

            throw $this->transportFailure(7);
        });
        [$user, $project] = $this->member();
        $url = "/api/v1/projects/{$project->project_id}/workflows/generate_report";

        $this->actingAs($user)->postJson($url, ['report_type' => 'ingestion_quality'])
            ->assertStatus(502)
            ->assertJsonPath('error', 'trigger_failed')
            ->assertJsonPath('message', 'The generate_report workflow could not be started right now. Please try again shortly.');
        $this->assertSame(2, $attempts, 'a refused connection never reached FastAPI, so it is worth a second try');

        // Nothing was dispatched, so the user's own retry is not a double click.
        $this->actingAs($user)->postJson($url, ['report_type' => 'ingestion_quality'])->assertStatus(502);
        $this->assertSame(4, $attempts);
    }

    public function test_a_read_timeout_is_not_retried_and_keeps_the_cooldown(): void
    {
        Sleep::fake();
        $attempts = 0;
        Http::fake(function () use (&$attempts) {
            $attempts++;

            throw $this->transportFailure(28);
        });
        [$user, $project] = $this->member();
        $url = "/api/v1/projects/{$project->project_id}/workflows/generate_report";

        // The request may have started a run whose reply was lost: post it once.
        $this->actingAs($user)->postJson($url, ['report_type' => 'ingestion_quality'])
            ->assertStatus(502)
            ->assertJsonPath('error', 'trigger_failed')
            ->assertJsonPath('message', 'The generate_report request was sent but no answer came back, so it may have started. Check its status before trying again.');
        $this->assertSame(1, $attempts);

        // ...and do not invite the second click that starts it twice. (Each click
        // mints a new request id, so FastAPI cannot tell the two apart.)
        $this->actingAs($user)->postJson($url, ['report_type' => 'ingestion_quality'])
            ->assertStatus(429)
            ->assertJsonPath('error', 'workflow_recently_triggered');
        $this->assertSame(1, $attempts, 'the second click must not reach FastAPI');
    }

    public function test_a_connect_failure_that_recovers_dispatches_once(): void
    {
        Sleep::fake();
        $attempts = 0;
        Http::fake(function (HttpRequest $request) use (&$attempts) {
            $attempts++;
            if ($attempts === 1) {
                throw $this->transportFailure(6);
            }

            return Http::response([
                'workflow' => 'generate_report', 'workflow_run_id' => 'run-second-try',
                'workspace_id' => $request['workspace_id'],
            ], 202);
        });
        [$user, $project] = $this->member();

        $this->actingAs($user)
            ->postJson("/api/v1/projects/{$project->project_id}/workflows/generate_report", [
                'report_type' => 'ingestion_quality',
            ])
            ->assertStatus(202)
            ->assertJsonPath('workflow_run_id', 'run-second-try');
        $this->assertSame(2, $attempts);
    }

    public function test_the_retry_posts_the_same_request_id(): void
    {
        // FastAPI dedupes on this id, so a retry must carry the one the first
        // attempt did, not a fresh one.
        Sleep::fake();
        $ids = [];
        Http::fake(function (HttpRequest $request) use (&$ids) {
            $ids[] = $request['input']['export_request_id'];
            if (count($ids) === 1) {
                throw $this->transportFailure(7);
            }

            return Http::response(['workflow' => 'generate_report', 'workflow_run_id' => 'run-x', 'workspace_id' => null], 202);
        });
        [$user, $project] = $this->member();

        $this->actingAs($user)
            ->postJson("/api/v1/projects/{$project->project_id}/workflows/generate_report", [
                'report_type' => 'ingestion_quality',
            ])
            ->assertStatus(202);

        $this->assertCount(2, $ids);
        $this->assertSame($ids[0], $ids[1]);
    }

    // ── Workspace-scoped admin workflows ────────────────────────────────

    public function test_workspace_trigger_requires_admin(): void
    {
        Http::fake();
        [$user, $project] = $this->member();

        $this->actingAs($user)
            ->postJson("/api/v1/admin/workspaces/{$project->workspace_id}/workflows/workspace_export")
            ->assertForbidden();

        Http::assertNothingSent();
    }

    public function test_admin_outside_the_workspace_gets_404(): void
    {
        Http::fake();
        [, $project] = $this->member();

        $this->actingAs(User::factory()->admin()->create())
            ->postJson("/api/v1/admin/workspaces/{$project->workspace_id}/workflows/workspace_export")
            ->assertNotFound()
            ->assertJsonPath('message', 'Workspace not found.');

        Http::assertNothingSent();
    }

    public function test_admin_member_exports_their_workspace(): void
    {
        $this->fakeAccepted('run-export');
        [$admin, $project] = $this->member(admin: true);
        $workspace = (string) $project->workspace_id;

        $this->actingAs($admin)
            ->postJson("/api/v1/admin/workspaces/{$workspace}/workflows/workspace_export", [
                'bucket' => 'somewhere-else',
            ])
            ->assertStatus(202)
            ->assertJson(['workflow' => 'workspace_export', 'workflow_run_id' => 'run-export', 'workspace_id' => $workspace]);

        Http::assertSent(fn (HttpRequest $request): bool => $request->url() === self::BASE.'workspace_export/trigger'
            && $request['workspace_id'] === $workspace
            && $request['input'] === ['workspace_id' => $workspace]);
    }

    public function test_restore_requires_an_own_workspace_manifest_and_confirmation_to_write(): void
    {
        $this->fakeAccepted('run-restore');
        [$admin, $project] = $this->member(admin: true);
        $workspace = (string) $project->workspace_id;
        $url = "/api/v1/admin/workspaces/{$workspace}/workflows/restore_workspace";
        $bucket = self::EXPORTS_BUCKET;
        $own = "s3://{$bucket}/workspace-exports/{$workspace}/2026-09-29T000000-r.jsonl.gz";

        $this->actingAs($admin)->postJson($url, [
            'snapshot_manifest_uri' => "s3://{$bucket}/workspace-exports/a0000000-0000-0000-0000-00000000dead/x.jsonl.gz",
        ])->assertUnprocessable()->assertJsonValidationErrors('snapshot_manifest_uri');

        $this->actingAs($admin)->postJson($url, [
            'snapshot_manifest_uri' => "s3://{$bucket}/workspace-exports/{$workspace}/../other/x.jsonl.gz",
        ])->assertUnprocessable()->assertJsonValidationErrors('snapshot_manifest_uri');

        // The layouts that are no longer an export of this deployment: a bucket
        // of its own named workspace-exports (Terraform never creates it), the
        // exports bucket without the key prefix, and some other bucket.
        foreach ([
            "s3://workspace-exports/{$workspace}/2026-09-29T000000-r.jsonl.gz",
            "s3://{$bucket}/{$workspace}/2026-09-29T000000-r.jsonl.gz",
            "s3://georag-backups-123456789012/workspace-exports/{$workspace}/2026-09-29T000000-r.jsonl.gz",
        ] as $refused) {
            $this->actingAs($admin)->postJson($url, ['snapshot_manifest_uri' => $refused])
                ->assertUnprocessable()->assertJsonValidationErrors('snapshot_manifest_uri');
        }

        $this->actingAs($admin)->postJson($url, ['snapshot_manifest_uri' => $own, 'dry_run' => false])
            ->assertUnprocessable()
            ->assertJsonValidationErrors('confirm_workspace_id');
        Http::assertNothingSent();

        $this->actingAs($admin)->postJson($url, ['snapshot_manifest_uri' => $own])->assertStatus(202);
        $this->actingAs($admin)->postJson($url, [
            'snapshot_manifest_uri' => $own, 'dry_run' => false, 'confirm_workspace_id' => $workspace,
        ])->assertStatus(202);

        $sent = Http::recorded()->map(fn (array $pair): array => $pair[0]['input']);
        $this->assertTrue($sent[0]['dry_run']);
        $this->assertFalse($sent[1]['dry_run']);
        $this->assertSame($admin->getKey(), $sent[1]['initiated_by_user_id']);
    }

    public function test_lineage_walk_of_the_workspace_targets_that_workspace(): void
    {
        $this->fakeAccepted();
        [$admin, $project] = $this->member(admin: true);
        $workspace = (string) $project->workspace_id;

        $this->actingAs($admin)
            ->postJson("/api/v1/admin/workspaces/{$workspace}/workflows/lineage_walk", [
                'target_type' => 'workspace',
                'target_id' => 'a0000000-0000-0000-0000-00000000dead',
            ])
            ->assertStatus(202);

        Http::assertSent(fn (HttpRequest $request): bool => $request['input']['kwargs'] === [
            'target_type' => 'workspace', 'target_id' => $workspace, 'limit' => 1000,
        ] && $request['input']['actor_id'] === $admin->getKey());
    }

    public function test_support_replay_is_always_a_dry_run(): void
    {
        $this->fakeAccepted();
        [$admin, $project] = $this->member(admin: true);

        $this->actingAs($admin)
            ->postJson("/api/v1/admin/workspaces/{$project->workspace_id}/workflows/support_replay", [
                'ticket_id' => 'C1000000-0000-0000-0000-000000000020',
                'original_workflow_run_id' => 'wf-1',
                'dry_run' => false,
            ])
            ->assertStatus(202);

        Http::assertSent(fn (HttpRequest $request): bool => $request['input']['dry_run'] === true
            && $request['input']['ticket_id'] === 'c1000000-0000-0000-0000-000000000020'
            && $request['workspace_id'] === (string) $project->workspace_id);
    }

    public function test_support_packet_carries_the_actor_as_requester(): void
    {
        $this->fakeAccepted();
        [$admin, $project] = $this->member(admin: true);

        $this->actingAs($admin)
            ->postJson("/api/v1/admin/workspaces/{$project->workspace_id}/workflows/support_packet_assemble", [
                'incident_id' => 'INC-42',
            ])
            ->assertStatus(202);

        Http::assertSent(fn (HttpRequest $request): bool => $request['input']['kwargs'] === [
            'incident_id' => 'INC-42', 'requested_by' => $admin->getKey(),
        ]);
    }

    // ── Platform-wide ───────────────────────────────────────────────────

    public function test_incident_diagnosis_is_admin_only_and_platform_wide(): void
    {
        $this->fakeAccepted('run-diag');

        $this->actingAs(User::factory()->create())
            ->postJson('/api/v1/admin/workflows/llm_incident_diagnosis_run', ['alert_label' => 'HighErrorRate'])
            ->assertForbidden();
        Http::assertNothingSent();

        $admin = User::factory()->admin()->create();
        $this->actingAs($admin)
            ->postJson('/api/v1/admin/workflows/llm_incident_diagnosis_run', [
                'alert_label' => 'HighErrorRate',
                'window_minutes' => 30,
            ])
            ->assertStatus(202)
            ->assertJsonPath('workflow_run_id', 'run-diag');

        Http::assertSent(fn (HttpRequest $request): bool => $request['workspace_id'] === null
            && $request['input']['workspace_id'] === null
            && $request['input']['kwargs'] === ['alert_label' => 'HighErrorRate', 'window_minutes' => 30]);
    }
}
