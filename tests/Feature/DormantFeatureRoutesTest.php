<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * LAR-10 / LAR-11 (2026-09-29 audit): two routed features with no UI that
 * answered 500/503 in AWS are gated behind config flags that default OFF.
 *
 *  - Cloud-ingest OAuth (/oauth/*): GET /oauth/connections ran
 *    `CREATE TABLE IF NOT EXISTS silver.cloud_ingest_connections` on the
 *    request path, which georag_app may not do (500), and the table would
 *    have had no RLS. The DDL is gone.
 *  - Admin integrations (/admin/integrations/*): read AUDIT_ENCRYPTION_KEY
 *    with env(), unset in AWS (503).
 *
 * Off → 404 exactly like a missing route. On → the controller runs.
 */
final class DormantFeatureRoutesTest extends TestCase
{
    use RefreshDatabase;

    private const UUID = '00000000-0000-0000-0000-000000000000';

    /**
     * @return array<string, array{0: string, 1: string}>
     */
    public static function oauthRoutes(): array
    {
        return [
            'authorize' => ['GET', '/oauth/googledrive/authorize'],
            'callback' => ['GET', '/oauth/sharepoint/callback?code=x&state=y'],
            'connections' => ['GET', '/oauth/connections'],
        ];
    }

    /**
     * @return array<string, array{0: string, 1: string}>
     */
    public static function integrationRoutes(): array
    {
        return [
            'sender toggle' => ['PATCH', '/admin/integrations/senders/'.self::UUID.'/disable'],
            'jwt key rotate' => ['POST', '/admin/integrations/jwt-keys/rotate'],
            'sender register' => ['POST', '/admin/integrations/senders'],
            'sender hmac rotate' => ['POST', '/admin/integrations/senders/'.self::UUID.'/rotate-hmac'],
        ];
    }

    #[DataProvider('oauthRoutes')]
    public function test_oauth_routes_are_404_by_default(string $method, string $url): void
    {
        $this->assertFalse(config('services.cloud_ingest_oauth.enabled'));

        $this->actingAs(User::factory()->create())
            ->json($method, $url)
            ->assertNotFound();
    }

    #[DataProvider('integrationRoutes')]
    public function test_admin_integration_routes_are_404_by_default_even_for_an_admin(string $method, string $url): void
    {
        $this->assertFalse(config('services.admin_integrations.enabled'));

        $admin = User::factory()->create();
        $admin->forceFill(['is_admin' => true])->save();

        $this->actingAs($admin)
            ->json($method, $url)
            ->assertNotFound();
    }

    public function test_oauth_connections_never_runs_ddl_and_reports_an_unprovisioned_table(): void
    {
        config(['services.cloud_ingest_oauth.enabled' => true]);

        DB::enableQueryLog();
        DB::flushQueryLog();

        $this->actingAs(User::factory()->create())
            ->getJson('/oauth/connections')
            ->assertStatus(503)
            ->assertJsonPath('error', 'cloud ingest connections are not provisioned');

        foreach (DB::getQueryLog() as $query) {
            $this->assertStringNotContainsStringIgnoringCase('CREATE TABLE', $query['query']);
        }
    }

    public function test_oauth_authorize_without_a_client_id_is_503_not_500(): void
    {
        config([
            'services.cloud_ingest_oauth.enabled' => true,
            'services.cloud_ingest_oauth.providers.googledrive.client_id' => null,
        ]);

        $this->actingAs(User::factory()->create())
            ->get('/oauth/googledrive/authorize')
            ->assertStatus(503);
    }

    public function test_oauth_authorize_reads_the_client_id_from_config(): void
    {
        config([
            'services.cloud_ingest_oauth.enabled' => true,
            'services.cloud_ingest_oauth.providers.googledrive.client_id' => 'test-client-id',
        ]);

        $response = $this->actingAs(User::factory()->create())
            ->get('/oauth/googledrive/authorize');

        $response->assertRedirect();
        $this->assertStringStartsWith('https://accounts.google.com/o/oauth2/v2/auth?', (string) $response->headers->get('Location'));
        $this->assertStringContainsString('client_id=test-client-id', (string) $response->headers->get('Location'));
    }

    public function test_admin_integrations_still_require_the_admin_gate_when_enabled(): void
    {
        config(['services.admin_integrations.enabled' => true]);

        $this->actingAs(User::factory()->create())
            ->json('PATCH', '/admin/integrations/senders/'.self::UUID.'/disable')
            ->assertForbidden();
    }
}
