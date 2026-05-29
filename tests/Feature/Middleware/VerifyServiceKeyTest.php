<?php

declare(strict_types=1);

namespace Tests\Feature\Middleware;

use App\Http\Middleware\VerifyServiceKey;
use Illuminate\Support\Facades\Route;
use Tests\TestCase;

/**
 * Phase H4 §7 — verify the FastAPI → Laravel callback service-key gate.
 *
 * The middleware protects /api/internal/* endpoints that exist purely for
 * service-to-service calls (Reverb broadcast bridge). It enforces a
 * shared secret in X-Service-Key, constant-time compared.
 */
final class VerifyServiceKeyTest extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();
        Route::middleware(VerifyServiceKey::class)
            ->any('/_test/service-key/echo', function () {
                return response()->json(['ok' => true], 200);
            });
    }

    public function test_request_without_header_is_rejected(): void
    {
        config(['app.env' => 'testing']);
        putenv('FASTAPI_SERVICE_KEY=correct-horse-battery-staple');

        $resp = $this->get('/_test/service-key/echo');

        $resp->assertStatus(401);
        $resp->assertJson(['error' => 'invalid service key']);

        putenv('FASTAPI_SERVICE_KEY');
    }

    public function test_request_with_mismatched_key_is_rejected(): void
    {
        putenv('FASTAPI_SERVICE_KEY=correct-horse-battery-staple');

        $resp = $this->withHeaders(['X-Service-Key' => 'wrong'])
            ->get('/_test/service-key/echo');

        $resp->assertStatus(401);
        $resp->assertJson(['error' => 'invalid service key']);

        putenv('FASTAPI_SERVICE_KEY');
    }

    public function test_request_with_matching_key_is_allowed(): void
    {
        putenv('FASTAPI_SERVICE_KEY=correct-horse-battery-staple');

        $resp = $this->withHeaders(['X-Service-Key' => 'correct-horse-battery-staple'])
            ->get('/_test/service-key/echo');

        $resp->assertOk();
        $resp->assertJson(['ok' => true]);

        putenv('FASTAPI_SERVICE_KEY');
    }

    public function test_empty_env_key_blocks_all_requests(): void
    {
        // No FASTAPI_SERVICE_KEY in env — even a request supplying a key fails.
        putenv('FASTAPI_SERVICE_KEY');

        $resp = $this->withHeaders(['X-Service-Key' => 'anything'])
            ->get('/_test/service-key/echo');

        $resp->assertStatus(401);
    }

    public function test_empty_string_key_blocks_all_requests(): void
    {
        // Env key is set but empty — must still reject.
        putenv('FASTAPI_SERVICE_KEY=');

        $resp = $this->withHeaders(['X-Service-Key' => ''])
            ->get('/_test/service-key/echo');

        $resp->assertStatus(401);

        putenv('FASTAPI_SERVICE_KEY');
    }
}
