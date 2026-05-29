<?php

declare(strict_types=1);

namespace App\Services;

use Firebase\JWT\JWT;
use RuntimeException;

/**
 * Mints short-lived HS256 JWTs for Laravel -> FastAPI service-to-service auth.
 *
 * Motivation (B7): the previous static X-Service-Key header gave FastAPI no
 * way to know which user a request was running under, which made per-user /
 * per-document RBAC impossible on the FastAPI side. Every outbound FastAPI
 * call now carries an `Authorization: Bearer <jwt>` token whose claims
 * identify the acting user, project, and role set. FastAPI validates the
 * signature with the same shared secret (FASTAPI_SERVICE_KEY) and trusts the
 * enclosed identity for authorisation decisions.
 *
 * Octane safety: the class holds no state. `mint()` reads the signing key
 * from config at call time and returns a freshly encoded string — no
 * request-scoped data leaks into a long-lived worker process.
 */
final class FastApiJwtMinter
{
    /**
     * Token TTL in seconds. Deliberately short: a request-scoped token is
     * only valid long enough to reach FastAPI and finish the call. Clock
     * skew between Laravel and FastAPI containers is assumed <1 s on the
     * shared Docker host, so 60 s is a comfortable upper bound.
     */
    private const TTL_SECONDS = 60;

    private const ISSUER = 'georag-laravel';

    private const AUDIENCE = 'georag-fastapi';

    private const ALGO = 'HS256';

    /**
     * Minimum key size (bytes) for HS256 HMAC per RFC 7518 §3.2 — below this
     * PyJWT emits InsecureKeyLengthWarning and the key is brute-forceable.
     * Matches the FastAPI-side validator in src/fastapi/app/config.py.
     */
    private const MIN_SECRET_BYTES = 32;

    /**
     * Mint a service JWT for a single outbound FastAPI call.
     *
     * @param int|string $userId Acting user (QueryAuditLog.user_id).
     * @param string $projectId Project scope (UUID).
     * @param array<int, string> $roles Role names; empty array when a
     *                                  full roles system isn't wired
     *                                  up yet. FastAPI treats an empty
     *                                  list as "no elevated permissions".
     * @param string|null $workspaceId Optional workspace UUID. When
     *                                 provided, embedded as a top-
     *                                 level `workspace_id` claim that
     *                                 FastAPI's UserContext picks up
     *                                 directly. Omit to let FastAPI
     *                                 derive it from project_id via
     *                                 services.workspace_resolution.
     *
     * @return string Compact-serialised JWT.
     */
    public function mint(
        int|string $userId,
        string $projectId,
        array $roles = [],
        ?string $workspaceId = null,
    ): string {
        $secret = config('services.fastapi.service_key');

        if (! is_string($secret) || $secret === '') {
            // Fail loud — this path cannot silently fall back to unsigned
            // tokens. An empty secret would let any attacker forge an
            // identity claim.
            throw new RuntimeException(
                'FastApiJwtMinter: services.fastapi.service_key is not configured.',
            );
        }

        // R13 — mirror the FastAPI-side length check so a weak key fails
        // at mint time on Laravel as well, not just at validation time on
        // FastAPI. Prevents a half-rotated deployment from silently
        // signing weak tokens that the receiver then accepts.
        $secretBytes = strlen($secret);
        if ($secretBytes < self::MIN_SECRET_BYTES) {
            throw new RuntimeException(sprintf(
                'FastApiJwtMinter: FASTAPI_SERVICE_KEY is %d bytes — must be >= %d for HS256 JWT signing. '
                    ."Generate a new one with: python3 -c 'import secrets; print(secrets.token_urlsafe(48))'",
                $secretBytes,
                self::MIN_SECRET_BYTES,
            ));
        }

        $now = time();

        $payload = [
            'iss' => self::ISSUER,
            'aud' => self::AUDIENCE,
            'sub' => (string) $userId,
            'project_id' => $projectId,
            'roles' => array_values($roles),
            'iat' => $now,
            'exp' => $now + self::TTL_SECONDS,
        ];

        if ($workspaceId !== null && $workspaceId !== '') {
            $payload['workspace_id'] = $workspaceId;
        }

        // V1.5-03 — `kid` header identifies which signing key minted the
        // token. FastAPI's auth path looks the kid up in a kid→secret map
        // (current + previous overlap during rotation). Default 'primary'
        // matches the FastAPI `FASTAPI_SERVICE_KEY_KID` default; operators
        // change both env vars together when rotating.
        $kid = (string) (config('services.fastapi.service_key_kid') ?: 'primary');
        $headers = ['kid' => $kid];

        return JWT::encode($payload, $secret, self::ALGO, null, $headers);
    }
}
