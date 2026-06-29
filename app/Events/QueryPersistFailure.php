<?php

declare(strict_types=1);

namespace App\Events;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Broadcast when the agentic-retrieval lineage write fails after all
 * `_insert_answer_run_with_retry` attempts are exhausted.
 *
 * Context: `persist_node` in
 * src/fastapi/app/agent/agentic_retrieval/nodes.py already retries the
 * silver.answer_runs INSERT three times with exponential backoff. When
 * the third retry still fails, the answer has been streamed to the
 * caller but the audit row is missing. The agent escalates to Loki via
 * `logger.error(..., extra={"alert": True})` and bumps the
 * AGENTIC_PERSIST_FAILURES Prometheus counter — but there's no signal
 * to the user that their answer wasn't written to the audit trail.
 *
 * This event is the user-visible channel. FastAPI's stream forwarder
 * can fire it via the existing Laravel webhook bridge; the React Chat
 * page subscribes to `query.{queryId}` (the same channel the answer
 * stream uses, registered in routes/channels.php) and renders a small
 * banner so the operator knows the answer exists but the audit trail
 * doesn't.
 *
 * Channel + transport notes
 * -------------------------
 * - Uses `query.{queryId}` — NOT a new "query.streaming.*" pattern —
 *   so subscriptions are auth'd by the existing channel callback in
 *   routes/channels.php. The `runId` (silver.answer_runs.run_id) is
 *   carried in the payload for operators that want to look up the
 *   missing row.
 * - ShouldBroadcastNow so the event flushes inline alongside the
 *   already-streamed answer; queueing would let the user close the
 *   tab before the banner ever renders.
 *
 * Wiring is intentionally OUT OF SCOPE for the audit pass — this class
 * just exists so the bridge / frontend can be wired without a Laravel
 * round-trip when that work happens. See
 * docs/handover/AUDIT_AND_FIX_REPORT.md P2-C.
 */
class QueryPersistFailure implements ShouldBroadcastNow
{
    use Dispatchable;
    use InteractsWithSockets;
    use SerializesModels;

    public function __construct(
        public readonly string $queryId,
        public readonly ?string $runId = null,
        public readonly bool $recoverable = false,
        public readonly string $message = 'This answer was not recorded in the audit trail.',
    ) {}

    /**
     * @return array<int, PrivateChannel>
     */
    public function broadcastOn(): array
    {
        return [new PrivateChannel("query.{$this->queryId}")];
    }

    public function broadcastAs(): string
    {
        return 'QueryPersistFailure';
    }

    /**
     * @return array<string, mixed>
     */
    public function broadcastWith(): array
    {
        return [
            'query_id' => $this->queryId,
            'run_id' => $this->runId,
            'recoverable' => $this->recoverable,
            'message' => $this->message,
        ];
    }
}
