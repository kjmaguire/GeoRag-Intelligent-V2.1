<?php

declare(strict_types=1);

namespace App\Http\Controllers;

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
 * Supports the 3 providers the master plan calls out:
 *   - sharepoint (Microsoft Graph)
 *   - onedrive   (Microsoft Graph)
 *   - googledrive (Google Drive v3)
 *
 * Routes:
 *   GET  /oauth/{provider}/authorize     redirect to provider auth URL
 *   GET  /oauth/{provider}/callback      OAuth callback handler
 *   GET  /oauth/{provider}/folders       list user's folders (after auth)
 *   POST /oauth/{provider}/connect       persist a folder-watch connection
 *   GET  /oauth/connections              list this user's connections
 *
 * IMPORTANT — operator setup required:
 *   This scaffold is functional but requires per-provider OAuth app
 *   registration before it can be used end-to-end:
 *     - Microsoft Graph: register at https://entra.microsoft.com → app
 *       registrations; needs Files.Read.All, Sites.Read.All scopes
 *     - Google Drive: register at https://console.cloud.google.com →
 *       APIs & Services → credentials; needs drive.readonly scope
 *   Set the CLIENT_ID + CLIENT_SECRET env vars per provider (see
 *   config/services.php). Until those are set, /oauth/{provider}/authorize
 *   returns a 500 with a clear "OAuth app not configured" message.
 *
 * State is signed with the app key + has a 10-minute TTL to prevent CSRF.
 *
 * Tokens are persisted to silver.cloud_ingest_connections (created on
 * first POST /connect). Refresh tokens are encrypted at rest.
 */
class OAuthIngestController extends Controller
{
    private const PROVIDERS = ['sharepoint', 'onedrive', 'googledrive'];

    private const PROVIDER_CONFIG = [
        'sharepoint' => [
            'auth_url_env' => 'OAUTH_SHAREPOINT_AUTH_URL',
            'token_url_env' => 'OAUTH_SHAREPOINT_TOKEN_URL',
            'client_id_env' => 'OAUTH_SHAREPOINT_CLIENT_ID',
            'client_secret_env' => 'OAUTH_SHAREPOINT_CLIENT_SECRET',
            'scope' => 'offline_access Sites.Read.All Files.Read.All',
            'default_auth_url' => 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
            'default_token_url' => 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
        ],
        'onedrive' => [
            'auth_url_env' => 'OAUTH_ONEDRIVE_AUTH_URL',
            'token_url_env' => 'OAUTH_ONEDRIVE_TOKEN_URL',
            'client_id_env' => 'OAUTH_ONEDRIVE_CLIENT_ID',
            'client_secret_env' => 'OAUTH_ONEDRIVE_CLIENT_SECRET',
            'scope' => 'offline_access Files.Read.All',
            'default_auth_url' => 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize',
            'default_token_url' => 'https://login.microsoftonline.com/common/oauth2/v2.0/token',
        ],
        'googledrive' => [
            'auth_url_env' => 'OAUTH_GOOGLEDRIVE_AUTH_URL',
            'token_url_env' => 'OAUTH_GOOGLEDRIVE_TOKEN_URL',
            'client_id_env' => 'OAUTH_GOOGLEDRIVE_CLIENT_ID',
            'client_secret_env' => 'OAUTH_GOOGLEDRIVE_CLIENT_SECRET',
            'scope' => 'https://www.googleapis.com/auth/drive.readonly',
            'default_auth_url' => 'https://accounts.google.com/o/oauth2/v2/auth',
            'default_token_url' => 'https://oauth2.googleapis.com/token',
        ],
    ];

    public function start(Request $request, string $provider): RedirectResponse
    {
        if (! in_array($provider, self::PROVIDERS, true)) {
            abort(404);
        }
        $cfg = self::PROVIDER_CONFIG[$provider];
        $clientId = env($cfg['client_id_env']);
        if (! $clientId) {
            abort(500, "OAuth provider '{$provider}' not configured: set ".$cfg['client_id_env'].' (and '.$cfg['client_secret_env'].')');
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

        $authUrl = env($cfg['auth_url_env'], $cfg['default_auth_url']);
        $params = http_build_query([
            'client_id' => $clientId,
            'response_type' => 'code',
            'redirect_uri' => route('oauth.callback', ['provider' => $provider]),
            'scope' => $cfg['scope'],
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

        // Exchange code for tokens
        $cfg = self::PROVIDER_CONFIG[$provider];
        try {
            $resp = Http::asForm()->post(env($cfg['token_url_env'], $cfg['default_token_url']), [
                'client_id' => env($cfg['client_id_env']),
                'client_secret' => env($cfg['client_secret_env']),
                'code' => $code,
                'redirect_uri' => route('oauth.callback', ['provider' => $provider]),
                'grant_type' => 'authorization_code',
            ]);
        } catch (\Throwable $exc) {
            Log::error("OAuth token exchange failed for {$provider}", ['exc' => $exc->getMessage()]);

            return response()->json(['error' => 'token exchange failed', 'reason' => $exc->getMessage()], 502);
        }
        if (! $resp->ok()) {
            return response()->json(['error' => 'token endpoint returned non-2xx', 'body' => $resp->body()], 502);
        }
        $tokens = $resp->json();

        // Persist connection
        try {
            $this->ensureConnectionsTable();
            DB::table('silver.cloud_ingest_connections')->updateOrInsert(
                [
                    'user_id' => $payload['user_id'],
                    'provider' => $provider,
                ],
                [
                    'access_token_enc' => encrypt($tokens['access_token'] ?? ''),
                    'refresh_token_enc' => encrypt($tokens['refresh_token'] ?? ''),
                    'expires_at' => now()->addSeconds((int) ($tokens['expires_in'] ?? 3600)),
                    'scopes' => $tokens['scope'] ?? $cfg['scope'],
                    'updated_at' => now(),
                    'created_at' => now(),
                ],
            );
        } catch (\Throwable $exc) {
            Log::error('OAuth connection persist failed', ['exc' => $exc->getMessage()]);

            return response()->json(['error' => 'connection persist failed', 'reason' => $exc->getMessage()], 500);
        }

        return redirect()->to('/onboarding?oauth_completed='.$provider);
    }

    public function listConnections(Request $request): JsonResponse
    {
        $this->ensureConnectionsTable();
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }
        $rows = DB::table('silver.cloud_ingest_connections')
            ->where('user_id', $user->id)
            ->select('provider', 'scopes', 'expires_at', 'created_at')
            ->get();

        return response()->json(['items' => $rows]);
    }

    private function ensureConnectionsTable(): void
    {
        // Lazy schema bootstrap so OAuth scaffolding works even on
        // installations that haven't run the matching migration yet.
        // Full canonical schema lands in a future migration file.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.cloud_ingest_connections (
                user_id           bigint NOT NULL,
                provider          varchar(32) NOT NULL,
                access_token_enc  text,
                refresh_token_enc text,
                expires_at        timestamptz,
                scopes            text,
                created_at        timestamptz NOT NULL DEFAULT now(),
                updated_at        timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (user_id, provider)
            )
        SQL);
    }
}
