<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Models\User;
use Illuminate\Auth\Events\PasswordReset;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Hash;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Password as PasswordBroker;
use Illuminate\Support\Str;
use Illuminate\Validation\Rules\Password;
use Laravel\Sanctum\PersonalAccessToken;

/**
 * Sanctum token-based authentication for the GeoRAG API.
 *
 * Endpoints:
 *   POST /api/v1/auth/register  — create account + issue token
 *   POST /api/v1/auth/login     — issue token for existing account
 *   POST /api/v1/auth/logout    — revoke current token
 *   GET  /api/v1/auth/me        — authenticated user profile + projects
 */
class AuthController extends Controller
{
    /**
     * Send a password reset link without revealing account existence.
     */
    public function forgotPassword(Request $request): JsonResponse
    {
        $validated = $request->validate([
            'email' => ['required', 'string', 'email', 'max:255'],
        ]);

        // Production runs MAIL_MAILER=log, so sendResetLink() writes the
        // reset URL to the log stream and returns success. The user was told
        // "a link has been sent", waited, and received nothing — and because
        // the message deliberately does not reveal account existence, neither
        // they nor support could tell "no such account" from "mail was never
        // configured". Account recovery in production meant editing the
        // database by hand.
        //
        // Saying so is not an existence leak: whether mail works is a
        // property of the deployment, not of the address typed in.
        if (! $this->mailIsDeliverable()) {
            Log::critical('Password reset requested but no mailer is configured.', [
                'event' => 'mail.not_configured',
                'mailer' => config('mail.default'),
            ]);

            return response()->json([
                'message' => 'Password reset by email is not available on this deployment. '
                    .'Ask an administrator to reset your password.',
            ], 503);
        }

        PasswordBroker::sendResetLink([
            'email' => $validated['email'],
        ]);

        return response()->json([
            'message' => 'If an account exists for that email, a password reset link has been sent.',
        ]);
    }

    /**
     * Whether a reset email would actually leave the building.
     *
     * `log` and `null` accept a message and drop it — `log` is what
     * production is set to. `array` is deliberately NOT on this list: it
     * keeps the message in memory where a test can assert on it, which is a
     * delivery a test can observe.
     */
    private function mailIsDeliverable(): bool
    {
        return ! in_array(
            (string) config('mail.default'),
            ['log', 'null'],
            true,
        );
    }

    /**
     * Validate a reset token and replace the account password.
     */
    public function resetPassword(Request $request): JsonResponse
    {
        $validated = $request->validate([
            'token' => ['required', 'string'],
            'email' => ['required', 'string', 'email', 'max:255'],
            'password' => ['required', 'confirmed', Password::min(8)],
        ]);

        $status = PasswordBroker::reset(
            $validated,
            function (User $user, string $password): void {
                $user->forceFill([
                    'password' => Hash::make($password),
                    'remember_token' => Str::random(60),
                ])->save();

                event(new PasswordReset($user));
            },
        );

        if ($status !== PasswordBroker::PASSWORD_RESET) {
            return response()->json([
                'message' => __($status),
            ], 422);
        }

        return response()->json([
            'message' => __($status),
        ]);
    }

    /**
     * Register a new user and issue a Sanctum API token.
     */
    public function register(Request $request): JsonResponse
    {
        // Closed by default. Anyone could create an account here and the
        // account was not inert: project creation used to fall back to a
        // hardcoded workspace, which put a stranger inside the production
        // tenant with read access to its audit ledger and usage rollups.
        // There is no registration UI, so nothing legitimate calls this
        // outside tests and local bootstrapping.
        if (! config('auth.registration_open', false)) {
            return response()->json([
                'message' => 'Registration is closed. Ask an administrator for an account.',
            ], 403);
        }

        $validated = $request->validate([
            'name' => ['required', 'string', 'max:255'],
            'email' => ['required', 'string', 'email', 'max:255', 'unique:users,email'],
            'password' => ['required', 'string', Password::min(8)],
        ]);

        $user = User::create([
            'name' => $validated['name'],
            'email' => $validated['email'],
            'password' => Hash::make($validated['password']),
        ]);

        $token = $user->createToken('georag-api')->plainTextToken;

        return response()->json([
            'user' => [
                'id' => $user->id,
                'name' => $user->name,
                'email' => $user->email,
            ],
            'token' => $token,
        ], 201);
    }

    /**
     * Authenticate and issue a Sanctum API token.
     */
    public function login(Request $request): JsonResponse
    {
        $validated = $request->validate([
            'email' => ['required', 'string', 'email'],
            'password' => ['required', 'string'],
        ]);

        $user = User::where('email', $validated['email'])->first();

        if (! $user || ! Hash::check($validated['password'], $user->password)) {
            return response()->json([
                'message' => 'Invalid credentials.',
            ], 401);
        }

        // Revoke previous tokens for this device to prevent token sprawl.
        $user->tokens()->where('name', 'georag-api')->delete();
        $token = $user->createToken('georag-api')->plainTextToken;

        return response()->json([
            'user' => [
                'id' => $user->id,
                'name' => $user->name,
                'email' => $user->email,
            ],
            'token' => $token,
        ]);
    }

    /**
     * Revoke the current API token OR terminate the SPA session, whichever
     * authenticated the request.
     *
     * Previous implementation unconditionally called
     * `$request->user()->currentAccessToken()->delete()`. That works for
     * Bearer-token callers but throws `BadMethodCallException` for Sanctum
     * SPA cookie users because `currentAccessToken()` returns an instance of
     * `Laravel\Sanctum\TransientToken` (not a persisted PersonalAccessToken)
     * which has no `delete()` method. The SPA's frontend auto-401 handler
     * (resources/js/bootstrap.ts) calls this endpoint on every session
     * expiry, so every single SPA logout was surfacing a 500.
     *
     * We now detect the auth style at request time and take the right
     * tear-down path:
     *   - PersonalAccessToken → revoke just this token
     *   - TransientToken      → Auth::guard('web')->logout() + session reset
     */
    public function logout(Request $request): JsonResponse
    {
        $token = $request->user()?->currentAccessToken();

        if ($token instanceof PersonalAccessToken) {
            $token->delete();
        } else {
            // Session-authenticated caller (SPA cookie). Invalidate the
            // session and rotate the CSRF token so a replay of the cookie
            // can't be used to re-authenticate.
            Auth::guard('web')->logout();
            if ($request->hasSession()) {
                $request->session()->invalidate();
                $request->session()->regenerateToken();
            }
        }

        // RequestGuard caches the resolved user per process. In FPM each
        // request is a fresh process so this is irrelevant, but under Octane
        // (and within a single test method that issues multiple HTTP calls)
        // the cache would cause a revoked token to keep authenticating.
        Auth::forgetGuards();

        return response()->json([
            'message' => 'Logged out.',
        ]);
    }

    /**
     * SPA cookie-based login via session auth.
     *
     * The React SPA should first GET /sanctum/csrf-cookie to prime the
     * XSRF-TOKEN cookie, then POST here with credentials. Sanctum's
     * EnsureFrontendRequestsAreStateful middleware handles the rest.
     */
    public function spaLogin(Request $request): JsonResponse
    {
        $validated = $request->validate([
            'email' => ['required', 'string', 'email'],
            'password' => ['required', 'string'],
        ]);

        if (! Auth::attempt(['email' => $validated['email'], 'password' => $validated['password']], $request->boolean('remember'))) {
            return response()->json([
                'message' => 'Invalid credentials.',
            ], 401);
        }

        $request->session()->regenerate();

        $user = $request->user();

        return response()->json([
            'user' => [
                'id' => $user->id,
                'name' => $user->name,
                'email' => $user->email,
            ],
        ]);
    }

    /**
     * SPA session logout — invalidates the session and rotates the CSRF token.
     */
    public function spaLogout(Request $request): JsonResponse
    {
        Auth::guard('web')->logout();

        $request->session()->invalidate();
        $request->session()->regenerateToken();

        return response()->json([
            'message' => 'Logged out.',
        ]);
    }

    /**
     * Return the authenticated user's profile and project memberships.
     */
    public function me(Request $request): JsonResponse
    {
        $user = $request->user();
        $projects = $user->projects()->get(['silver.projects.project_id', 'silver.projects.project_name']);

        return response()->json([
            'user' => [
                'id' => $user->id,
                'name' => $user->name,
                'email' => $user->email,
            ],
            'projects' => $projects->map(fn ($p) => [
                'project_id' => $p->project_id,
                'project_name' => $p->project_name,
                'role' => $p->pivot->role,
            ]),
        ]);
    }
}
