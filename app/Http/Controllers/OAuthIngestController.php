<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use Illuminate\Database\QueryException;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;

/**
 * §8.5 Step 3 (deferred branch) — OAuth flows for cloud-source ingestion.
 *
 * DORMANT, gated OFF (LAR-10, 2026-09-29). The routes sit behind
 * `services.cloud_ingest_oauth.enabled` (CLOUD_INGEST_OAUTH_ENABLED,
 * default false) and answer 404 while it is off. No page in
 * resources/js/Pages calls them. Before the gate, GET /oauth/connections
 * returned 500 unconditionally in AWS: it ran `CREATE TABLE IF NOT EXISTS`
 * on the request path, and georag_app has no CREATE on `silver`.
 *
 * Supports the 3 providers the master plan calls out:
 *   - sharepoint (Microsoft Graph)
 *   - onedrive   (Microsoft Graph)
 *   - googledrive (Google Drive v3)
 *
 * Routes (the only three that exist — `/folders` and `/connect`, which this
 * docblock used to list, were never registered):
 *   GET  /oauth/{provider}/authorize     redirect to provider auth URL
 *   GET  /oauth/{provider}/callback      OAuth callback handler
 *   GET  /oauth/connections              list this user's connections
 *
 * To turn it on an operator needs, in order:
 *   1. A migration creating silver.cloud_ingest_connections with FORCE ROW
 *      LEVEL SECURITY and a tenant_isolation policy (§06b). The runtime DDL
 *      that used to stand in for it is gone; until the table exists the
 *      connection reads answer 503 and the callback cannot persist.
 *   2. Per-provider OAuth app registration, with client id + secret set in
 *      config/services.php → cloud_ingest_oauth.providers (env
 *      OAUTH_{PROVIDER}_CLIENT_ID / _CLIENT_SECRET).
 *   3. CLOUD_INGEST_OAUTH_ENABLED=true.
 *
 * State is signed with the app key + has a 10-minute TTL to prevent CSRF.
 * Tokens are encrypted at rest with the app key.
 */
class OAuthIngestController extends Controller
{
    private const PROVIDERS = ['sharepoint', 'onedrive', 'googledrive'];

    private const SCOPES = [
        'sharepoint' => 'offline_access Sites.Read.All Files.Read.All',
        'onedrive' => 'offline_access Files.Read.All',
        'googledrive' => 'https://www.googleapis.com/auth/drive.readonly',
    ];

    public function start(Request $request, string $provider): RedirectResponse
    {
        if (! in_array($provider, self::PROVIDERS, true)) {
            abort(404);
        }
        $cfg = $this->providerConfig($provider);
        $clientId = $cfg['client_id'];
        if (! $clientId) {
            abort(503, "OAuth provider '{$provider}' is not configured.");
        }

        // State: signed payload {user_id, ts, project_id?} with 10-min TTL
        $state = base64_encode(json_encode([
            'user_id' => $request->user()?->id,
            'project_id' => (string) $request->query('project_id', ''),
            'provider' => $provider,
            'ts' => time(),
            'nonce' => Str::random(16),
        ]));
        $signature = hash_hmac('sha256', $state, config('app.key'));
        $signedState = "{$state}.{$signature}";
        $request->session()->put("oauth_state_{$provider}", $signedState);

        $authUrl = $cfg['auth_url'];
        $params = http_build_query([
            'client_id' => $clientId,
            'response_type' => 'code',
            'redirect_uri' => route('oauth.callback', ['provider' => $provider]),
            'scope' => self::SCOPES[$provider],
            'state' => $signedState,
            'access_type' => 'offline',
            'prompt' => 'consent',
        ]);

        return redirect("{$authUrl}?{$params}");
    }

    public function callback(Request $request, string $provider): RedirectResponse|JsonResponse
    {
        if (! in_array($provider, self::PROVIDERS, true)) {
            abort(404);
        }
        $code = $request->query('code');
        $returnedState = $request->query('state');
        $expected = $request->session()->pull("oauth_state_{$provider}");
        if (! $code || ! $returnedState || $returnedState !== $expected) {
            return response()->json(['error' => 'state mismatch or missing code'], 400);
        }
        [$payloadB64, $sig] = explode('.', $returnedState, 2) + [null, null];
        if (! hash_equals(hash_hmac('sha256', $payloadB64, config('app.key')), $sig ?? '')) {
            return response()->json(['error' => 'invalid state signature'], 400);
        }
        $payload = json_decode(base64_decode($payloadB64), true);
        if (! is_array($payload) || (time() - ($payload['ts'] ?? 0)) > 600) {
            return response()->json(['error' => 'state expired'], 400);
        }

        // Exchange code for tokens.
        //
        // The timeouts are the point: this was the only outbound HTTP call in
        // the application without one, and Guzzle's default is to wait
        // forever. It runs in the request path on a four-worker Octane
        // container with a single replica, so an identity provider that
        // accepts the connection and then stops responding takes a quarter of
        // the site's request capacity with it, per stuck callback, until the
        // container is restarted.
        $cfg = $this->providerConfig($provider);
        try {
            $resp = Http::asForm()
                ->connectTimeout(5)
                ->timeout(15)
                ->post($cfg['token_url'], [
                    'client_id' => $cfg['client_id'],
                    'client_secret' => $cfg['client_secret'],
                    'code' => $code,
                    'redirect_uri' => route('oauth.callback', ['provider' => $provider]),
                    'grant_type' => 'authorization_code',
                ]);
        } catch (\Throwable $exc) {
            Log::error("OAuth token exchange failed for {$provider}", ['exc' => $exc->getMessage()]);

            return response()->json(['error' => 'token exchange failed'], 502);
        }
        if (! $resp->ok()) {
            return response()->json(['error' => 'token endpoint returned non-2xx', 'status' => $resp->status()], 502);
        }
        $tokens = $resp->json();

        // Persist connection. The table is created by no migration yet (see
        // the class docblock); a missing table lands in the catch below.
        try {
            DB::table('silver.cloud_ingest_connections')->updateOrInsert(
                [
                    'user_id' => $payload['user_id'],
                    'provider' => $provider,
                ],
                [
                    'access_token_enc' => encrypt($tokens['access_token'] ?? ''),
                    'refresh_token_enc' => encrypt($tokens['refresh_token'] ?? ''),
                    'expires_at' => now()->addSeconds((int) ($tokens['expires_in'] ?? 3600)),
                    'scopes' => $tokens['scope'] ?? self::SCOPES[$provider],
                    'updated_at' => now(),
                    'created_at' => now(),
                ],
            );
        } catch (\Throwable $exc) {
            Log::error('OAuth connection persist failed', ['exc' => $exc->getMessage()]);

            return response()->json(['error' => 'connection persist failed'], 503);
        }

        return redirect()->to('/projects?oauth_completed='.$provider);
    }

    public function listConnections(Request $request): JsonResponse
    {
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        try {
            $rows = DB::table('silver.cloud_ingest_connections')
                ->where('user_id', $user->id)
                ->select('provider', 'scopes', 'expires_at', 'created_at')
                ->get();
        } catch (QueryException $exc) {
            Log::warning('OAuth connections unavailable', ['exc' => $exc->getMessage()]);

            return response()->json(['error' => 'cloud ingest connections are not provisioned'], 503);
        }

        return response()->json(['items' => $rows]);
    }

    /**
     * @return array{client_id: ?string, client_secret: ?string, auth_url: string, token_url: string}
     */
    private function providerConfig(string $provider): array
    {
        /** @var array{client_id: ?string, client_secret: ?string, auth_url: string, token_url: string} $cfg */
        $cfg = config("services.cloud_ingest_oauth.providers.{$provider}");

        return $cfg;
    }
}
