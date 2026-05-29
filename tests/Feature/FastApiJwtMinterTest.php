<?php

namespace Tests\Feature;

use App\Services\FastApiJwtMinter;
use Firebase\JWT\JWT;
use Firebase\JWT\Key;
use Firebase\JWT\SignatureInvalidException;
use Tests\TestCase;

/**
 * B7 — Laravel must mint short-TTL HS256 JWTs for outbound FastAPI calls
 * so FastAPI can see which user is behind each request. These tests pin
 * the wire-format contract so the FastAPI agent can rely on it.
 */
class FastApiJwtMinterTest extends TestCase
{
    private const TEST_SECRET = 'unit-test-signing-key-at-least-32b!!';

    protected function setUp(): void
    {
        parent::setUp();

        config(['services.fastapi.service_key' => self::TEST_SECRET]);
    }

    public function test_minted_token_decodes_with_same_secret(): void
    {
        $minter = new FastApiJwtMinter();

        $jwt = $minter->mint(42, 'proj-abc', ['member']);

        $decoded = JWT::decode($jwt, new Key(self::TEST_SECRET, 'HS256'));

        $this->assertSame('42', $decoded->sub);
        $this->assertSame('proj-abc', $decoded->project_id);
        $this->assertSame(['member'], (array) $decoded->roles);
        $this->assertSame('georag-laravel', $decoded->iss);
        $this->assertSame('georag-fastapi', $decoded->aud);
    }

    public function test_claims_include_iat_exp_and_ttl_is_sixty_seconds(): void
    {
        $minter = new FastApiJwtMinter();

        $jwt = $minter->mint('user-1', 'proj-xyz', []);
        $decoded = JWT::decode($jwt, new Key(self::TEST_SECRET, 'HS256'));

        $this->assertIsInt($decoded->iat);
        $this->assertIsInt($decoded->exp);
        $this->assertSame(60, $decoded->exp - $decoded->iat);
    }

    public function test_token_signed_with_different_secret_fails_to_decode(): void
    {
        $minter = new FastApiJwtMinter();

        $jwt = $minter->mint(1, 'proj-a', []);

        $this->expectException(SignatureInvalidException::class);
        JWT::decode($jwt, new Key('a-different-secret-that-should-reject', 'HS256'));
    }

    public function test_roles_default_to_empty_array(): void
    {
        $minter = new FastApiJwtMinter();

        $jwt = $minter->mint(7, 'proj-empty');
        $decoded = JWT::decode($jwt, new Key(self::TEST_SECRET, 'HS256'));

        $this->assertSame([], (array) $decoded->roles);
    }

    /**
     * V1.5-03 — every minted token carries `kid` in its header so FastAPI
     * can pick the matching key from a kid→secret map (rotation support).
     */
    public function test_minted_token_has_default_primary_kid_header(): void
    {
        $minter = new FastApiJwtMinter();
        $jwt = $minter->mint(1, 'proj-kid');

        // JWT header is the first '.' segment, base64url decoded.
        [$headerB64] = explode('.', $jwt);
        $header = json_decode(base64_decode(strtr($headerB64, '-_', '+/')), true);

        $this->assertSame('primary', $header['kid'] ?? null);
        $this->assertSame('HS256', $header['alg'] ?? null);
    }

    public function test_minted_token_carries_configured_kid(): void
    {
        config(['services.fastapi.service_key_kid' => 'rot-2026-q3']);

        $minter = new FastApiJwtMinter();
        $jwt = $minter->mint(1, 'proj-kid');

        [$headerB64] = explode('.', $jwt);
        $header = json_decode(base64_decode(strtr($headerB64, '-_', '+/')), true);
        $this->assertSame('rot-2026-q3', $header['kid'] ?? null);

        // Reset for any tests that follow.
        config(['services.fastapi.service_key_kid' => 'primary']);
    }
}
