<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Str;
use Illuminate\Testing\TestResponse;
use Tests\TestCase;

/**
 * POST /api/v1/auth/spa-login — the login path the SPA actually uses
 * (resources/js/Pages/Login.tsx posts here and nowhere else).
 *
 * Until 2026-10-10 nothing under tests/ exercised it: the only references were
 * OPTIONS preflights in CorsAllowlistTest, and the token-issuing sibling
 * /auth/login had a wrong-password 401 and nothing more. A regression in the
 * credential check, in session rotation (session fixation) or in the
 * `throttle:auth-login` middleware would have shipped green.
 *
 * Three behaviours are pinned, each the way a browser would observe it:
 *
 *   1. wrong credentials -> 401, and the caller is STILL a guest
 *   2. right credentials -> 200, authenticated, and the session id the client
 *      holds is replaced (the Set-Cookie carries a new id)
 *   3. the 6th attempt inside a minute -> 429, even with the right password.
 *      Limiter (AppServiceProvider::boot): 5/min keyed by sha1(email)|ip,
 *      shared with /auth/login.
 *
 * Two traps in driving a cookie session from a feature test, both of which
 * made a first draft of these tests pass for the wrong reason:
 *
 *   - Sanctum only starts a session for "stateful" requests, i.e. ones whose
 *     Origin/Referer matches config('sanctum.stateful'). A browser SPA always
 *     sends Origin on a POST, so browser() does too.
 *   - postJson()/getJson() send NO cookies unless withCredentials() is set.
 *     A planted session cookie was silently dropped, so "the id changed after
 *     login" held for every implementation. browser() sets it, and the
 *     fixation test checks the premise that the planted id is honoured.
 *
 * The app instance (and so the session Store) is shared by every request in a
 * test method. An authenticated follow-up that sends no cookie would still be
 * authenticated by attributes the Store kept in memory, so the follow-ups
 * flushSession() first and the credential has to come from the cookie.
 */
final class SpaLoginTest extends TestCase
{
    use RefreshDatabase;

    private const URL = '/api/v1/auth/spa-login';

    private const PASSWORD = 'correct-horse-Battery-staple-9';

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create([
            'email' => 'geologist@example.test',
            'password' => self::PASSWORD,
        ]);
    }

    /**
     * A request as the SPA sends it: same-origin Origin header, cookies carried,
     * and, when given, the session cookie the browser already holds.
     */
    private function browser(?string $sessionCookie = null): static
    {
        $this->defaultCookies = [];
        $this->withCredentials()->withHeaders(['Origin' => 'http://localhost']);

        if ($sessionCookie !== null) {
            $this->withCookie((string) config('session.cookie'), $sessionCookie);
        }

        return $this;
    }

    /**
     * @param array<string, mixed> $payload
     */
    private function spaLogin(array $payload, ?string $sessionCookie = null): TestResponse
    {
        return $this->browser($sessionCookie)->postJson(self::URL, $payload);
    }

    /**
     * GET /auth/me carrying ONLY the given session cookie: no shared in-memory
     * session state, no cached guard user.
     */
    private function whoAmI(?string $sessionCookie): TestResponse
    {
        $this->flushSession();
        $this->app['auth']->forgetGuards();

        return $this->browser($sessionCookie)->getJson('/api/v1/auth/me');
    }

    private function sessionIdIssuedBy(TestResponse $response): ?string
    {
        return $response->getCookie((string) config('session.cookie'))?->getValue();
    }

    public function test_wrong_password_is_401_and_the_caller_stays_a_guest(): void
    {
        $response = $this->spaLogin([
            'email' => $this->user->email,
            'password' => 'not-the-password',
        ]);

        $response->assertStatus(401)
            ->assertExactJson(['message' => 'Invalid credentials.']);
        $this->assertGuest();
    }

    public function test_unknown_email_is_the_same_401_as_a_wrong_password(): void
    {
        // No account-enumeration oracle: the two failures are indistinguishable.
        $wrongPassword = $this->spaLogin([
            'email' => $this->user->email,
            'password' => 'not-the-password',
        ]);
        $unknownEmail = $this->spaLogin([
            'email' => 'nobody@example.test',
            'password' => 'not-the-password',
        ]);

        $unknownEmail->assertStatus(401);
        $this->assertSame($wrongPassword->getContent(), $unknownEmail->getContent());
        $this->assertGuest();
    }

    public function test_correct_password_authenticates_the_user_and_returns_the_profile(): void
    {
        $response = $this->spaLogin([
            'email' => $this->user->email,
            'password' => self::PASSWORD,
        ]);

        $response->assertOk()
            ->assertJsonPath('user.id', $this->user->id)
            ->assertJsonPath('user.email', $this->user->email)
            ->assertJsonPath('user.name', $this->user->name);
        $this->assertAuthenticatedAs($this->user);

        $body = (string) $response->getContent();
        $this->assertStringNotContainsString('password', $body);
        $this->assertStringNotContainsString('remember_token', $body);
    }

    /**
     * Session fixation: an attacker who planted a known session id before
     * login must not keep it afterwards.
     *
     * The first half is the premise that keeps the second half honest. A
     * request that does not log in leaves the id alone, so the server really
     * is working with the id the client sent; a different id after the
     * successful login can therefore only come from the login rotating it. (If
     * the planted cookie were ignored, every response would carry a fresh id
     * and the assertion below would pass for any implementation.)
     *
     * Note what this does NOT pin: the explicit `$request->session()->
     * regenerate()` in AuthController::spaLogin. SessionGuard::login() — which
     * Auth::attempt() ends in — already calls regenerate(true), so deleting
     * that line changes nothing observable. The property is pinned, not the
     * line; it fails for a login that authenticates the session without
     * rotating it (a hand-rolled session()->put() login, a custom guard).
     */
    public function test_login_replaces_the_session_id_the_client_arrived_with(): void
    {
        $planted = Str::random(40);

        $failed = $this->spaLogin([
            'email' => $this->user->email,
            'password' => 'not-the-password',
        ], sessionCookie: $planted);
        $failed->assertStatus(401);
        $this->assertSame(
            $planted,
            $this->sessionIdIssuedBy($failed),
            'premise: the session id the client sends is the one the server keeps until something rotates it',
        );

        $response = $this->spaLogin([
            'email' => $this->user->email,
            'password' => self::PASSWORD,
        ], sessionCookie: $planted);

        $response->assertOk();
        $issued = $this->sessionIdIssuedBy($response);
        $this->assertNotNull($issued, 'login must set the session cookie');
        $this->assertNotSame($planted, $issued, 'the session id must be regenerated on login');
    }

    public function test_the_session_cookie_issued_by_login_is_the_credential(): void
    {
        $login = $this->spaLogin([
            'email' => $this->user->email,
            'password' => self::PASSWORD,
        ]);
        $sessionId = $this->sessionIdIssuedBy($login);
        $this->assertNotNull($sessionId);

        $this->whoAmI($sessionId)
            ->assertOk()
            ->assertJsonPath('user.id', $this->user->id);

        // The same request without the cookie is a stranger.
        $this->whoAmI(null)->assertUnauthorized();
    }

    public function test_a_failed_login_issues_no_authenticated_session(): void
    {
        $failed = $this->spaLogin([
            'email' => $this->user->email,
            'password' => 'not-the-password',
        ]);
        $failed->assertStatus(401);
        $sessionId = $this->sessionIdIssuedBy($failed);

        $this->whoAmI($sessionId)->assertUnauthorized();
    }

    public function test_credentials_are_required(): void
    {
        $this->spaLogin(['email' => $this->user->email])
            ->assertStatus(422)
            ->assertJsonValidationErrors(['password']);
        $this->spaLogin(['email' => 'not-an-email', 'password' => self::PASSWORD])
            ->assertStatus(422)
            ->assertJsonValidationErrors(['email']);
        $this->assertGuest();
    }

    public function test_the_sixth_attempt_in_a_minute_is_throttled_even_with_the_right_password(): void
    {
        for ($attempt = 1; $attempt <= 5; $attempt++) {
            $this->spaLogin([
                'email' => $this->user->email,
                'password' => 'guess-'.$attempt,
            ])->assertStatus(401);
        }

        $throttled = $this->spaLogin([
            'email' => $this->user->email,
            'password' => self::PASSWORD,
        ]);

        $throttled->assertStatus(429);
        $this->assertNotNull($throttled->headers->get('Retry-After'));
        $this->assertGuest();
    }

    public function test_the_throttle_is_per_email_not_global(): void
    {
        $other = User::factory()->create([
            'email' => 'someone.else@example.test',
            'password' => self::PASSWORD,
        ]);

        for ($attempt = 1; $attempt <= 6; $attempt++) {
            $this->spaLogin([
                'email' => $this->user->email,
                'password' => 'guess-'.$attempt,
            ]);
        }
        $this->spaLogin([
            'email' => $this->user->email,
            'password' => self::PASSWORD,
        ])->assertStatus(429);

        // Another credential from the same origin is a different bucket.
        $this->spaLogin([
            'email' => $other->email,
            'password' => self::PASSWORD,
        ])->assertOk();
    }

    public function test_spa_login_and_token_login_share_one_budget(): void
    {
        // routes/api.php: "a single attacker can't split the budget across two
        // endpoints". Three guesses at /auth/login plus two here exhaust it.
        for ($attempt = 1; $attempt <= 3; $attempt++) {
            $this->postJson('/api/v1/auth/login', [
                'email' => $this->user->email,
                'password' => 'guess-'.$attempt,
            ])->assertStatus(401);
        }
        for ($attempt = 4; $attempt <= 5; $attempt++) {
            $this->spaLogin([
                'email' => $this->user->email,
                'password' => 'guess-'.$attempt,
            ])->assertStatus(401);
        }

        $this->spaLogin([
            'email' => $this->user->email,
            'password' => self::PASSWORD,
        ])->assertStatus(429);
    }
}
