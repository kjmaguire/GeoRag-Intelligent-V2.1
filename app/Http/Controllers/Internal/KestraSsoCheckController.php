<?php

declare(strict_types=1);

namespace App\Http\Controllers\Internal;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Http\Response;
use Illuminate\Support\Facades\Auth;
use Illuminate\Support\Facades\Gate;

/**
 * Phase 6 Step 2 (R-P4-2) — forward_auth target for the Caddy edge that
 * fronts Kestra with native WebSocket support.
 *
 * Caddy's `forward_auth` directive subrequests this endpoint with the
 * inbound headers (Cookie + Authorization). On a 2xx response, Caddy
 * proxies the original request to Kestra and copies the X-Kestra-Auth
 * response header onto the upstream Authorization header — so Kestra
 * sees a basic-auth-credentialed request without the operator ever
 * typing the Kestra password.
 *
 * Auth shape:
 *  - `auth:web` lets browser session cookies through (same path the
 *    Phase 4 Step 2 Laravel passthrough uses).
 *  - `auth:sanctum` lets Personal Access Tokens through, for operator
 *    CLI use + the Phase 6 Step 2 verifier.
 *
 * The route is registered behind both middlewares so EITHER works; the
 * Gate then enforces `admin`. Returns 204 No Content on success (Caddy
 * only inspects the status code + copied headers), 401/403 otherwise.
 */
class KestraSsoCheckController extends Controller
{
    public function check(Request $request): Response
    {
        $user = Auth::user();
        if ($user === null) {
            return response('', 401);
        }
        if (! Gate::allows('admin', $user)) {
            return response('', 403);
        }

        $kestraUser = (string) config('services.kestra.basic_auth_user', '');
        $kestraPass = (string) config('services.kestra.basic_auth_password', '');
        if ($kestraUser === '' || $kestraPass === '') {
            // 503 lets Caddy distinguish "auth ok but Kestra creds missing"
            // from "user not authorized". Operator runbook covers this.
            return response('', 503);
        }

        $basic = 'Basic '.base64_encode($kestraUser.':'.$kestraPass);

        return response('', 204)->withHeaders([
            'X-Kestra-Auth' => $basic,
            // For audit / debugging — Caddy logs include this.
            'X-Sanctum-Identity' => (string) $user->getAuthIdentifier(),
        ]);
    }
}
