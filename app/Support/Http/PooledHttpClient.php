<?php

declare(strict_types=1);

namespace App\Support\Http;

use GuzzleHttp\Client as GuzzleClient;
use GuzzleHttp\Handler\CurlMultiHandler;
use GuzzleHttp\HandlerStack;
use Illuminate\Http\Client\Factory;
use Illuminate\Http\Client\PendingRequest;

/**
 * Per-base-URL Guzzle client pool, Octane-safe.
 *
 * Background: Laravel's HTTP facade (Illuminate\Http\Client\Factory) builds a
 * fresh Guzzle\Client on every call, which means a fresh curl handle with no
 * TCP keep-alive across requests. Under Octane this is wasted setup per call
 * to Martin, FastAPI, Neo4j, etc.
 *
 * This pool keeps one long-lived Guzzle client per base URL with TCP
 * keep-alive enabled at the curl layer. Octane workers reuse the pool across
 * requests in the same worker; the upstream socket survives between calls.
 *
 * Octane safety:
 *  - Singleton-bound, but $clients is bounded (max 16 base URLs); pruned LRU
 *    on overflow so memory can't grow unbounded.
 *  - The pool holds Guzzle\Client instances only — no PendingRequest, no
 *    request-scoped state, no headers/query params persisted on the client.
 *  - Each call returns a fresh PendingRequest backed by the shared client;
 *    headers, body, query params are applied per-request as usual.
 *
 * Not for the chat-streaming hot path — that uses native PHP stream_context_create
 * because Guzzle's stream eof() bug requires a workaround [[reverb-dual-purpose-env-trap]]
 * style. This pool is for non-streaming auxiliary HTTP only (TileProxy, Health,
 * Admin → FastAPI service calls, etc.).
 */
final class PooledHttpClient
{
    /** Maximum number of distinct base URLs cached at once. */
    private const MAX_CLIENTS = 16;

    /** @var array<string, GuzzleClient> base_uri (string) → Guzzle client */
    private array $clients = [];

    /** @var array<string, int> base_uri (string) → monotonically-increasing tick (LRU) */
    private array $lastUsed = [];

    private int $tick = 0;

    public function __construct(private readonly Factory $factory) {}

    /**
     * Get a PendingRequest backed by the pooled Guzzle client for $baseUrl.
     *
     * The returned PendingRequest is fresh — chain ->withHeaders(), ->timeout(),
     * ->get(), etc. on it as you would with Http::. The underlying Guzzle
     * client (and its persistent curl handle) is shared.
     */
    public function forBaseUrl(string $baseUrl, int $timeoutSeconds = 15): PendingRequest
    {
        $client = $this->clientFor($baseUrl);

        return $this->factory
            ->setClient($client)
            ->baseUrl($baseUrl)
            ->timeout($timeoutSeconds);
    }

    private function clientFor(string $baseUrl): GuzzleClient
    {
        $this->tick++;
        $this->lastUsed[$baseUrl] = $this->tick;

        if (isset($this->clients[$baseUrl])) {
            return $this->clients[$baseUrl];
        }

        if (count($this->clients) >= self::MAX_CLIENTS) {
            $this->evictLeastRecentlyUsed();
        }

        $this->clients[$baseUrl] = $this->buildGuzzleClient();

        return $this->clients[$baseUrl];
    }

    private function evictLeastRecentlyUsed(): void
    {
        $lru = null;
        $lruTick = PHP_INT_MAX;
        foreach ($this->lastUsed as $url => $tick) {
            if ($tick < $lruTick) {
                $lruTick = $tick;
                $lru = $url;
            }
        }
        if ($lru !== null) {
            unset($this->clients[$lru], $this->lastUsed[$lru]);
        }
    }

    private function buildGuzzleClient(): GuzzleClient
    {
        $stack = HandlerStack::create(new CurlMultiHandler);

        return new GuzzleClient([
            'handler' => $stack,
            'http_errors' => false,
            'connect_timeout' => 5,
            // The PendingRequest sets timeout per-call; this is just a safety
            // ceiling in case a caller forgets.
            'timeout' => 60,
            'curl' => [
                CURLOPT_TCP_KEEPALIVE => 1,
                CURLOPT_TCP_KEEPIDLE => 60,
                CURLOPT_TCP_KEEPINTVL => 60,
            ],
        ]);
    }
}
