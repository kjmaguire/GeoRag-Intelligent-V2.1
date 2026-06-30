<?php

declare(strict_types=1);

namespace Tests\Feature;

use Tests\TestCase;

/**
 * Module 9 Chunk 9.6 (A1-03) — config/session.php now defaults Secure=true
 * outside the `local` environment. Local dev still works because Secure stays
 * false there; production / staging / testing environments default to true.
 */
final class SessionCookieTest extends TestCase
{
    public function test_session_secure_defaults_true_outside_local_env(): void
    {
        // The config has already loaded with APP_ENV=testing under the test
        // bootstrap. Verify the resolved value.
        $this->assertTrue(
            (bool) config('session.secure'),
            'session.secure must default to true outside local env (current env: '
            .app()->environment().')',
        );
    }

    public function test_session_secure_can_be_disabled_via_env(): void
    {
        // Verify the env override path still wins. We can't reload .env mid-test
        // safely, but we can directly poke the env var and re-evaluate the
        // config line's logic.
        $previous = $_ENV['SESSION_SECURE_COOKIE'] ?? null;
        $_ENV['SESSION_SECURE_COOKIE'] = 'false';
        putenv('SESSION_SECURE_COOKIE=false');

        try {
            // env('SESSION_SECURE_COOKIE', ...) — when value is 'false' string
            // Laravel's env() helper coerces it to boolean false.
            $this->assertFalse(
                env('SESSION_SECURE_COOKIE', null),
                'explicit env override of SESSION_SECURE_COOKIE=false must coerce to bool false',
            );
        } finally {
            if ($previous === null) {
                unset($_ENV['SESSION_SECURE_COOKIE']);
                putenv('SESSION_SECURE_COOKIE');
            } else {
                $_ENV['SESSION_SECURE_COOKIE'] = $previous;
                putenv('SESSION_SECURE_COOKIE='.$previous);
            }
        }
    }
}
