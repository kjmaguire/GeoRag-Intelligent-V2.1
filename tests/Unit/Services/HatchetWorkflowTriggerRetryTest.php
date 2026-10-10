<?php

declare(strict_types=1);

namespace Tests\Unit\Services;

use App\Services\HatchetWorkflowTrigger;
use GuzzleHttp\Exception\ConnectException;
use GuzzleHttp\Psr7\Request as PsrRequest;
use Illuminate\Http\Client\ConnectionException;
use InvalidArgumentException;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;
use RuntimeException;

/**
 * 2026-10 Hatchet audit, finding 8. HatchetWorkflowTrigger re-posted a workflow
 * trigger on any ConnectionException, but Laravel raises that for EVERY
 * transport failure, a cURL "operation timed out" (28) after the request was
 * written included. A POST that had already started a run, and lost only its
 * reply, was sent again and started a second one. A retry is safe only when the
 * cURL errno proves nothing was sent: 6 (could not resolve host) or 7 (could
 * not connect).
 */
final class HatchetWorkflowTriggerRetryTest extends TestCase
{
    private function transportFailure(?int $errno): ConnectionException
    {
        $guzzle = new ConnectException(
            'cURL error '.($errno ?? 'unknown').': simulated',
            new PsrRequest('POST', 'http://fastapi.test/internal/v1/workflows/x/trigger'),
            null,
            $errno === null ? [] : ['errno' => $errno],
        );

        return new ConnectionException($guzzle->getMessage(), 0, $guzzle);
    }

    /**
     * @return array<string, array{0: int}>
     */
    public static function provenUnsent(): array
    {
        return [
            'could not resolve host' => [6],
            'could not connect' => [7],
        ];
    }

    /**
     * @return array<string, array{0: int}>
     */
    public static function mayHaveBeenSent(): array
    {
        return [
            'operation timed out' => [28],
            'ssl connect error' => [35],
            'empty reply from server' => [52],
            'receive failure' => [56],
        ];
    }

    #[DataProvider('provenUnsent')]
    public function test_a_failure_before_the_request_was_written_is_retryable(int $errno): void
    {
        $this->assertTrue(HatchetWorkflowTrigger::neverReachedFastApi($this->transportFailure($errno)));
    }

    #[DataProvider('mayHaveBeenSent')]
    public function test_a_failure_that_can_happen_after_the_request_was_written_is_not(int $errno): void
    {
        $this->assertFalse(HatchetWorkflowTrigger::neverReachedFastApi($this->transportFailure($errno)));
    }

    public function test_a_connection_exception_with_no_curl_context_proves_nothing(): void
    {
        $this->assertFalse(HatchetWorkflowTrigger::neverReachedFastApi($this->transportFailure(null)));
        $this->assertFalse(HatchetWorkflowTrigger::neverReachedFastApi(new ConnectionException('Connection refused')));
    }

    public function test_only_a_connection_exception_can_be_retryable(): void
    {
        $this->assertFalse(HatchetWorkflowTrigger::neverReachedFastApi(new RuntimeException('boom')));
        $this->assertFalse(HatchetWorkflowTrigger::neverReachedFastApi(
            new InvalidArgumentException('x', 0, $this->transportFailure(7)),
        ));
    }
}
