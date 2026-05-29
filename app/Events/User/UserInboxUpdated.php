<?php

declare(strict_types=1);

namespace App\Events\User;

use Illuminate\Broadcasting\InteractsWithSockets;
use Illuminate\Broadcasting\PrivateChannel;
use Illuminate\Contracts\Broadcasting\ShouldBroadcastNow;
use Illuminate\Foundation\Events\Dispatchable;
use Illuminate\Queue\SerializesModels;

/**
 * Per-user inbox update — Phase 3 of the real-time staleness fix.
 *
 * Drives the Foundry/Inbox page so new mentions, review requests, and
 * refused queries appear without manual refresh. Also gives the nav
 * shell a hook to update the inbox-badge counter without a full page
 * reload.
 *
 * Channel: private-App.Models.User.{userId}
 *   The Laravel-default user channel (registered in routes/channels.php
 *   line 8 with the standard $user->id === $id auth check). Reusing it
 *   avoids registering a new channel pattern.
 *
 * The three inbox source tables and their writers:
 *   - silver.collaboration_mentions       — written when chat or
 *                                            annotation surfaces @-mention
 *                                            a user.
 *   - silver.collaboration_review_requests — written when an admin or
 *                                            geologist asks for a review.
 *   - audit.query_audit_log (response_text NULL) — every query starts as
 *                                                  a refusal candidate; the
 *                                                  Horizon worker either
 *                                                  fills response_text
 *                                                  (success) or leaves it
 *                                                  NULL (terminal refusal).
 */
class UserInboxUpdated implements ShouldBroadcastNow
{
    use Dispatchable;
    use InteractsWithSockets;
    use SerializesModels;

    public const KIND_MENTION = 'mention';

    public const KIND_REVIEW = 'review';

    public const KIND_REFUSAL = 'refusal';

    /**
     * @param array<string, mixed> $payload Optional richer context — itemId, title, snippet, source page link, etc. Receivers use this as a hint, not a data transport.
     */
    public function __construct(
        public readonly int $userId,
        public readonly string $kind,
        public readonly int $countDelta = 1,
        public readonly array $payload = [],
    ) {}

    public function broadcastOn(): array
    {
        // Laravel's default user channel — established in
        // routes/channels.php with the canonical {id} placeholder.
        return [
            new PrivateChannel('App.Models.User.'.$this->userId),
        ];
    }

    public function broadcastAs(): string
    {
        return 'user.inbox_updated';
    }

    /**
     * @return array{
     *   user_id: int,
     *   kind: string,
     *   count_delta: int,
     *   payload: array<string, mixed>,
     *   updated_at: string
     * }
     */
    public function broadcastWith(): array
    {
        return [
            'user_id' => $this->userId,
            'kind' => $this->kind,
            'count_delta' => $this->countDelta,
            'payload' => $this->payload,
            'updated_at' => now()->toIso8601String(),
        ];
    }
}
