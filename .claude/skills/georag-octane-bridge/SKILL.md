---
name: georag-octane-bridge
description: Laravel↔FastAPI HTTP bridge patterns for GeoRAG. Use when writing HTTP client calls from Laravel to FastAPI, forwarding chunked-transfer streams to Reverb, minting service-to-service JWTs, configuring Octane-safe HTTP clients, or selecting LLM backends. Triggers on tasks involving Http::client, Guzzle, FastApiJwtMinter, query streaming, /v1/query, /v1/retrieve, /v1/viz/compute, LLM_BACKEND, MAX_VALIDATION_RETRIES, kid header rotation, or chunked-transfer forwarding to Reverb broadcast channels.
metadata:
  origin: GeoRAG project (cross-cutting hard rules #2, #3 + Section 07c/07d)
  authoritative-sources:
    - georag-architecture.html §07c (streaming transport / Reverb event lifecycle)
    - georag-architecture.html §07d (Laravel↔FastAPI contracts + auth)
    - georag-architecture.html §08 (LLM backend selection, retries)
    - georag-architecture.html §05 (deterministic query flow)
    - CLAUDE.md hard rules #2, #3
  scope: Laravel-side HTTP client and stream forwarding only. FastAPI-side handler patterns belong to backend-fastapi agent.
---

# GeoRAG Laravel↔FastAPI Bridge (Octane edition)

Laravel owns the auth surface and the streaming transport. FastAPI never speaks directly to the browser — every byte of an answer flows through Laravel. Under Octane this means the HTTP client is long-lived, the connection pool persists, and any per-request state in a singleton will leak into the next request unless you do this correctly.

## When to Apply

Reference this skill when working on:
- HTTP calls from Laravel to FastAPI (`Http::`, Guzzle, custom clients)
- Streaming response forwarding (chunked-transfer → Reverb broadcasts)
- Horizon jobs that orchestrate the query lifecycle
- Service-to-service JWT issuance (`FastApiJwtMinter`)
- LLM backend selection (`LLM_BACKEND` env, retry budget)
- Anything in `app/Services/FastApi*`, `app/Http/Clients/`, `app/Jobs/Query*`
- Reverb channel auth + broadcast for `query.*` events

## The contract Laravel speaks to FastAPI (§07d)

| Endpoint | Use |
|---|---|
| `POST /v1/query` | Returns chunked stream of `query.token` → `query.body_done` → `query.citation_attached` (×N) → `query.validated` → `query.complete` → `query.suggestions`. Refusal branch substitutes `query.refusal`. |
| `POST /v1/retrieve` | Synchronous JSON. Returns chunks/entities/structured_rows + retrieval_trace_id. Use when frontend wants raw retrieval, not synthesis. |
| `POST /v1/viz/compute` | Synchronous JSON. Returns plotly_spec / image_b64 / data_table. |
| `GET /v1/health` | Synchronous JSON. Use for readiness probes only — don't gate user requests on it. |

All four require:
- `Authorization: Bearer <jwt>` minted via `FastApiJwtMinter` with `kid` header
- Request body MUST include `workspace_id`, `user_id`, `rbac_scopes[]`
- FastAPI **does not trust** caller-supplied identity — it re-checks workspace isolation against RBAC scopes on every store query

## Octane safety for HTTP clients (hard rule #3)

Under Octane, the application boots once. Reusing connections is good (lower latency); leaking per-request state is fatal (cross-request data exposure).

### ✅ Correct — keep the client connection-pool, scope per-request configuration

```php
<?php

declare(strict_types=1);

namespace App\Services\FastApi;

use Illuminate\Http\Client\Factory as HttpFactory;
use Illuminate\Http\Client\PendingRequest;

final class FastApiClient
{
    public function __construct(
        private readonly HttpFactory $http,
        private readonly FastApiJwtMinter $jwt,
    ) {}

    public function forUser(int $userId, int $workspaceId, array $rbacScopes): PendingRequest
    {
        // Fresh PendingRequest per call — never reuse across requests.
        return $this->http
            ->withToken($this->jwt->mintForUser($userId, $workspaceId, $rbacScopes))
            ->withHeaders([
                'X-Request-ID' => request()->header('X-Request-ID') ?? (string) str()->uuid(),
            ])
            ->baseUrl(config('services.fastapi.base_url'))
            ->timeout(30)
            ->connectTimeout(3);
    }
}
```

### ❌ Forbidden — singleton holding a request-bound client

```php
// DO NOT DO THIS under Octane — leaks user context to next request
$this->app->singleton(FastApiClient::class, fn ($app) =>
    new FastApiClient(
        $app['http']->withToken($app['request']->user()->fastApiToken)  // ❌
    )
);

// DO NOT DO THIS — accumulates forever
class FastApiClient {
    private static array $cachedTokens = [];  // ❌
}
```

### ✅ Correct — use `scoped()` if you need per-request memoization

```php
$this->app->scoped(QueryOrchestrator::class, function () {
    return new QueryOrchestrator(
        client: app(FastApiClient::class),
        workspaceId: fn () => request()->user()->currentWorkspace->id,  // resolver closure
    );
});
```

## Streaming pattern: FastAPI → Laravel → Reverb (§07c Option A)

The browser never opens a connection to FastAPI. Laravel reads the chunked stream and broadcasts each event to a Reverb private channel keyed on `answer_run_id`.

```php
<?php

declare(strict_types=1);

namespace App\Jobs\Query;

use App\Events\Query\QueryCitationAttached;
use App\Events\Query\QueryComplete;
use App\Events\Query\QueryRefusal;
use App\Events\Query\QueryToken;
use App\Events\Query\QueryValidated;
use App\Services\FastApi\FastApiClient;
use Illuminate\Bus\Queueable;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Bus\Dispatchable;
use Illuminate\Queue\InteractsWithQueue;
use Illuminate\Queue\SerializesModels;

final class StreamQueryFromFastApi implements ShouldQueue
{
    use Dispatchable, InteractsWithQueue, Queueable, SerializesModels;

    public int $tries = 1;          // streaming jobs are NOT retried — each attempt would re-prompt the LLM
    public int $timeout = 120;
    public string $queue = 'query'; // separate queue from ingestion (Horizon supervisor block per §07b)

    public function __construct(
        public readonly string $answerRunId,
        public readonly string $queryId,
        public readonly int $userId,
        public readonly int $workspaceId,
        public readonly array $rbacScopes,
        public readonly string $query,
    ) {}

    public function handle(FastApiClient $api): void
    {
        $response = $api->forUser($this->userId, $this->workspaceId, $this->rbacScopes)
            ->withOptions(['stream' => true])
            ->post('/v1/query', [
                'answer_run_id' => $this->answerRunId,
                'query_id'      => $this->queryId,
                'query'         => $this->query,
                'workspace_id'  => $this->workspaceId,
                'user_id'       => $this->userId,
                'rbac_scopes'   => $this->rbacScopes,
                'stream'        => true,
            ]);

        if ($response->failed()) {
            event(new QueryRefusal($this->answerRunId, $this->queryId, 'fastapi_unreachable', $response->status()));
            return;
        }

        // Read newline-delimited JSON chunks from the body stream.
        $body = $response->toPsrResponse()->getBody();
        $buffer = '';

        while (! $body->eof()) {
            $buffer .= $body->read(8192);

            while (($newline = strpos($buffer, "\n")) !== false) {
                $line = substr($buffer, 0, $newline);
                $buffer = substr($buffer, $newline + 1);

                if ($line === '') continue;

                $event = json_decode($line, associative: true, flags: JSON_THROW_ON_ERROR);
                $this->dispatchLifecycleEvent($event);
            }
        }
    }

    private function dispatchLifecycleEvent(array $event): void
    {
        match ($event['type']) {
            'query.token'              => event(new QueryToken($event)),
            'query.body_done'          => event(new \App\Events\Query\QueryBodyDone($event)),
            'query.citation_attached'  => event(new QueryCitationAttached($event)),
            'query.validated'          => event(new QueryValidated($event)),  // see georag-rag-citations skill
            'query.complete'           => event(new QueryComplete($event)),
            'query.suggestions'        => event(new \App\Events\Query\QuerySuggestions($event)),
            'query.refusal'            => event(new QueryRefusal($event)),
            default                    => logger()->warning('Unknown lifecycle event', $event),
        };
    }
}
```

The Reverb broadcast layer subscribes to these events and forwards to `private-answer-run.{answer_run_id}`. Lifecycle gating (don't forward `query.complete` without prior `query.validated`) lives in the listener — see the `georag-rag-citations` skill for that pattern.

## Service-to-service JWT (§07d, V1.5-03)

The `FastApiJwtMinter` stamps a `kid` header on every token. Multiple `kid` slots support zero-downtime rotation.

```php
<?php

declare(strict_types=1);

namespace App\Services\FastApi;

use Firebase\JWT\JWT;

final class FastApiJwtMinter
{
    public function __construct(
        private readonly array $config,  // injected, not request-bound — Octane-safe
    ) {}

    public function mintForUser(int $userId, int $workspaceId, array $rbacScopes): string
    {
        $kid = $this->config['active_kid'] ?? 'primary';
        $secret = $this->config['secrets'][$kid] ?? throw new \RuntimeException("Unknown kid: {$kid}");

        return JWT::encode(
            payload: [
                'iss'           => 'georag-laravel',
                'aud'           => 'georag-fastapi',
                'iat'           => time(),
                'exp'           => time() + 60,           // short TTL — service-to-service
                'sub'           => "user:{$userId}",
                'workspace_id'  => $workspaceId,
                'rbac_scopes'   => $rbacScopes,
            ],
            key: $secret,
            alg: 'HS256',
            keyId: $kid,                                   // ← the rotation lever
        );
    }
}
```

**Rotation:** add new secret under `previous` slot in FastAPI config, rotate active to new `kid` in Laravel config, wait for old tokens to expire (60s), remove `previous` slot. Procedure: `ops/runbooks/secret-rotation.md` § FASTAPI_SERVICE_KEY.

## Backend selection & retry budget (§08)

The `LLM_BACKEND` env var picks the inference path; `MAX_VALIDATION_RETRIES` bounds §04i guard retries.

| Scenario | `LLM_BACKEND` | `MAX_VALIDATION_RETRIES` |
|---|---|---|
| Dev / single-workstation | `ollama` | 2 (default) |
| Air-gapped on-prem | `vllm` | 2 (default) |
| Premium / milestone-gate | `anthropic` (Opus 4.7) | **1** — Opus self-corrects in the first call; second retry is wasted spend |

Laravel does not pick the backend itself — that's a FastAPI config — but Laravel surfaces the choice in admin UI and routes high-stakes queries (e.g., `qp_review` intent) through the premium path.

## Anti-patterns

| ❌ Don't | ✅ Do |
|---|---|
| Open a long-lived connection from React directly to FastAPI | All traffic goes React → Laravel → FastAPI. Browser never sees FastAPI. |
| Reuse `PendingRequest` across HTTP calls in Octane | Build fresh per call inside `forUser()` |
| `tries = 3` on the streaming Horizon job | Streaming jobs do **not** retry — each attempt re-prompts the LLM and burns tokens. Use `tries = 1` and surface failures via `query.refusal`. |
| Mint a long-lived JWT (hours) | Service tokens have 60-second TTL. The `kid` rotation pattern depends on short TTLs. |
| Trust caller-supplied `workspace_id` in `/v1/query` payload | FastAPI re-validates against RBAC scopes. Laravel still includes it for traceability — but do not skip Laravel-side workspace check. |
| Block the request thread on the chunked stream | Stream forwarding belongs in a Horizon job, not a controller action. Controller responds 202 with `answer_run_id`; React subscribes to Reverb. |

## Validation checkpoints

| Stage | Command | Expected |
|---|---|---|
| HTTP client builds | `php artisan test --filter=FastApiClientTest` | Token present in headers, base URL correct, no shared state |
| JWT minting | `php artisan test --filter=FastApiJwtMinterTest` | `kid` header present, TTL ≤ 60s, RBAC scopes included |
| Stream parsing | `php artisan test --filter=StreamQueryFromFastApiTest` | All 7 lifecycle event types dispatch correctly, refusal path tested |
| Octane safety | `vendor/bin/phpstan analyse --level=max app/Services/FastApi app/Jobs/Query` | No static-state warnings |
| Reverb broadcast auth | `php artisan test --filter=AnswerRunChannelTest` | Only the originating user + workspace members can subscribe to `private-answer-run.{id}` |

## Cross-references

- **Citation persistence on stream events:** read `georag-rag-citations` SKILL — covers the lifecycle gate listener and refusal-as-200 pattern
- **Schema validation for response payloads:** read `georag-schema-contracts` SKILL — covers Eloquent persistence of citation graphs
- **MCP server pattern (different transport, same auth concerns):** read `laravel-mcp` SKILL