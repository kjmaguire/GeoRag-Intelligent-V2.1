<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Models\User;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Hash;
use Illuminate\Validation\Rules\Password;

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
     * Register a new user and issue a Sanctum API token.
     */
    public function register(Request $request): JsonResponse
    {
        $validated = $request->validate([
            'name'     => ['required', 'string', 'max:255'],
            'email'    => ['required', 'string', 'email', 'max:255', 'unique:users,email'],
            'password' => ['required', 'string', Password::min(8)],
        ]);

        $user = User::create([
            'name'     => $validated['name'],
            'email'    => $validated['email'],
            'password' => Hash::make($validated['password']),
        ]);

        $token = $user->createToken('georag-api')->plainTextToken;

        return response()->json([
            'user'  => [
                'id'    => $user->id,
                'name'  => $user->name,
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
            'email'    => ['required', 'string', 'email'],
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
            'user'  => [
                'id'    => $user->id,
                'name'  => $user->name,
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

        if ($token instanceof \Laravel\Sanctum\PersonalAccessToken) {
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
            'email'    => ['required', 'string', 'email'],
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
                'id'    => $user->id,
                'name'  => $user->name,
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
                'id'    => $user->id,
                'name'  => $user->name,
                'email' => $user->email,
            ],
            'projects' => $projects->map(fn ($p) => [
                'project_id'   => $p->project_id,
                'project_name' => $p->project_name,
                'role'         => $p->pivot->role,
            ]),
        ]);
    }
}
