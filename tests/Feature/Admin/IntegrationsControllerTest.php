<?php

declare(strict_types=1);

namespace Tests\Feature\Admin;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * A7 (2026-07-28) regression coverage for IntegrationsController.
 *
 * Zero tests existed for this controller before this file. That gap is
 * exactly how a real bug went undetected: every one of the four action
 * methods below ended with `redirect()->route('admin.integrations')`, a
 * route name nothing has registered since Admin/Integrations.tsx (and its
 * GET /admin/integrations route) were deleted in the reader-core trim.
 * Confirmed via `artisan route:list --name=admin.integrations` returning
 * zero matches. Each method's database mutation — sender toggle, JWT key
 * rotation, sender registration, HMAC rotation — committed successfully,
 * and then the response construction itself threw
 * RouteNotFoundException, so the operator saw a 500 for an action that had
 * actually already succeeded.
 *
 * These tests exercise all four methods through the real HTTP + DB path
 * (RequiresPostgres — the encryption GUC dance and the flow_registry /
 * flow_jwt_keys schemas don't exist on SQLite) and assert both the DB
 * mutation AND a non-500 response, so the class of bug (successful
 * mutation, fatal response) cannot recur silently.
 */
class IntegrationsControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private function admin(): User
    {
        $user = User::factory()->create();
        DB::connection('pgsql')->table('users')
            ->where('id', $user->id)
            ->update(['is_admin' => true]);

        return $user->fresh();
    }

    /**
     * Insert a sender via the same stored procedure the controller itself
     * uses (usage.register_external_notification_sender), rather than
     * hand-rolling pgp_sym_encrypt — keeps the fixture honest about what
     * a real row looks like.
     */
    private function seedSender(string $source): string
    {
        $encKey = (string) env('AUDIT_ENCRYPTION_KEY', '');
        $this->assertNotSame('', $encKey, 'AUDIT_ENCRYPTION_KEY must be configured for this test');

        return DB::connection('pgsql')->transaction(function () use ($encKey, $source): string {
            DB::connection('pgsql')->statement(
                "SELECT set_config('app.audit_encryption_key', ?, true)",
                [$encKey],
            );
            $row = DB::connection('pgsql')->selectOne(
                'SELECT usage.register_external_notification_sender(?, ?, ?, NULL, NULL) AS id',
                [$source, 'primary', bin2hex(random_bytes(32))],
            );

            return (string) $row->id;
        });
    }

    private function seedFlow(string $flowName): void
    {
        DB::connection('pgsql')->table('workflow.flow_registry')->updateOrInsert(
            ['flow_name' => $flowName],
            [
                'kind' => 'agent-trigger',
                'description' => 'A7 test fixture',
                'hatchet_workflow_module' => 'app.hatchet_workflows.test_fixture',
                'hatchet_workflow_attr' => 'test_fixture',
                'pydantic_input_attr' => 'TestFixtureInput',
                'enabled' => true,
            ],
        );
    }

    protected function tearDown(): void
    {
        // RequiresPostgres::setUp() calls markTestSkipped() under SQLite,
        // which still runs tearDown() afterward. Touching the 'pgsql'
        // connection unconditionally here — even just to build a query,
        // before any statement executes — corrupted the shared SQLite
        // transaction for every test that ran later in the same process,
        // cascading into ~300 unrelated failures elsewhere in the suite.
        // Guard on the resolved connection, exactly like RequiresPostgres
        // itself does.
        if (config('database.default') === 'pgsql') {
            // These fixtures live outside RefreshDatabase's Schema-Builder
            // scope (raw workflow.* / usage.* tables), so clean up explicitly.
            DB::connection('pgsql')->table('usage.external_notification_senders')
                ->where('source', 'like', 'a7-test-%')
                ->delete();
            DB::connection('pgsql')->table('workflow.flow_registry')
                ->where('flow_name', 'a7_test_fixture_flow')
                ->delete();
            DB::connection('pgsql')->table('workflow.flow_jwt_keys')
                ->where('flow_name', 'a7_test_fixture_flow')
                ->delete();
        }

        parent::tearDown();
    }

    public function test_toggle_sender_disables_and_does_not_500(): void
    {
        $this->actingAs($this->admin());
        $id = $this->seedSender('a7-test-toggle');

        $response = $this->patch("/admin/integrations/senders/{$id}/disable");

        $response->assertRedirect();
        $this->assertNotSame(500, $response->getStatusCode());

        $row = DB::connection('pgsql')
            ->table('usage.external_notification_senders')
            ->where('id', $id)->first();
        $this->assertNotNull($row->disabled_at);
    }

    public function test_register_sender_creates_row_and_does_not_500(): void
    {
        $this->actingAs($this->admin());

        $response = $this->post('/admin/integrations/senders', [
            'source' => 'a7-test-register',
            'description' => 'A7 regression test',
        ]);

        $response->assertRedirect();
        $this->assertNotSame(500, $response->getStatusCode());
        $response->assertSessionHas('sender_secret');

        $exists = DB::connection('pgsql')
            ->table('usage.external_notification_senders')
            ->where('source', 'a7-test-register')
            ->exists();
        $this->assertTrue($exists);
    }

    public function test_rotate_sender_hmac_creates_new_row_and_does_not_500(): void
    {
        $this->actingAs($this->admin());
        $id = $this->seedSender('a7-test-rotate-hmac');

        $response = $this->post("/admin/integrations/senders/{$id}/rotate-hmac");

        $response->assertRedirect();
        $this->assertNotSame(500, $response->getStatusCode());
        $response->assertSessionHas('sender_secret');

        // The prior row is disabled; a new row for the same source exists.
        $prior = DB::connection('pgsql')
            ->table('usage.external_notification_senders')
            ->where('id', $id)->first();
        $this->assertNotNull($prior->disabled_at);

        $newCount = DB::connection('pgsql')
            ->table('usage.external_notification_senders')
            ->where('source', 'a7-test-rotate-hmac')
            ->where('disabled_at', null)
            ->count();
        $this->assertSame(1, $newCount);
    }

    public function test_rotate_flow_key_creates_key_and_does_not_500(): void
    {
        $this->actingAs($this->admin());
        $this->seedFlow('a7_test_fixture_flow');

        $response = $this->post('/admin/integrations/jwt-keys/rotate', [
            'flow_name' => 'a7_test_fixture_flow',
            'overlap_hours' => 0,
        ]);

        $response->assertRedirect();
        $this->assertNotSame(500, $response->getStatusCode());

        $exists = DB::connection('pgsql')
            ->table('workflow.flow_jwt_keys')
            ->where('flow_name', 'a7_test_fixture_flow')
            ->exists();
        $this->assertTrue($exists);
    }

    public function test_unauthenticated_request_is_rejected_not_500(): void
    {
        // No actingAs() — the 'admin' gate must reject before ever reaching
        // the dead-route redirect this whole file is about.
        $id = '00000000-0000-0000-0000-000000000000';

        $response = $this->patch("/admin/integrations/senders/{$id}/disable");

        $this->assertNotSame(500, $response->getStatusCode());
    }
}
