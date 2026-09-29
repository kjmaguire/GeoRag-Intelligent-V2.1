<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\RateLimiter;
use Tests\TestCase;

/**
 * POST /api/v1/client-errors — the React ErrorBoundary's telemetry target,
 * which had no route at all (FE-22, 2026-09-29).
 */
final class ClientErrorControllerTest extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();
        RateLimiter::clear('client-errors');
    }

    public function test_it_logs_one_structured_line_and_returns_204(): void
    {
        Log::spy();

        $this->withHeader('Sec-Fetch-Site', 'same-origin')
            ->postJson('/api/v1/client-errors', [
                'scope' => 'root',
                'message' => 'Cannot read properties of undefined',
                'stack' => "TypeError: x\n    at Workspace",
                'componentStack' => "\n    at FoundryWorkspace",
                'url' => 'https://georag.example/password/reset/SECRET-TOKEN?email=a@b.c',
                'userAgent' => 'Mozilla/5.0',
            ])
            ->assertNoContent();

        Log::shouldHaveReceived('warning')->once()->withArgs(function (string $message, array $context): bool {
            return $message === 'client.render_error'
                && $context['scope'] === 'root'
                && $context['message'] === 'Cannot read properties of undefined'
                // Query string dropped: it can carry tokens or return_to.
                && $context['path'] === '/password/reset/SECRET-TOKEN'
                && ! str_contains((string) json_encode($context), 'email=a@b.c')
                && $context['user_id'] === null;
        });
    }

    public function test_long_fields_are_truncated(): void
    {
        Log::spy();

        $this->withHeader('Sec-Fetch-Site', 'same-origin')
            ->postJson('/api/v1/client-errors', [
                'message' => str_repeat('m', 5000),
                'stack' => str_repeat('s', 20000),
            ])
            ->assertNoContent();

        Log::shouldHaveReceived('warning')->once()->withArgs(
            fn (string $message, array $context): bool => mb_strlen($context['message']) <= 1003
                && mb_strlen($context['stack']) <= 8003,
        );
    }

    public function test_oversized_payload_is_rejected(): void
    {
        $this->withHeader('Sec-Fetch-Site', 'same-origin')
            ->postJson('/api/v1/client-errors', ['stack' => str_repeat('s', 60000)])
            ->assertStatus(422);
    }

    public function test_it_is_throttled(): void
    {
        for ($i = 0; $i < 10; $i++) {
            $this->withHeader('Sec-Fetch-Site', 'same-origin')
                ->postJson('/api/v1/client-errors', ['message' => 'x'])
                ->assertNoContent();
        }

        $this->withHeader('Sec-Fetch-Site', 'same-origin')
            ->postJson('/api/v1/client-errors', ['message' => 'x'])
            ->assertStatus(429);
    }
}
