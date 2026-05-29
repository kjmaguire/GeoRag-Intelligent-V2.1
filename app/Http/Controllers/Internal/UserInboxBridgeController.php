<?php

declare(strict_types=1);

namespace App\Http\Controllers\Internal;

use App\Events\User\UserInboxUpdated;
use App\Http\Controllers\Controller;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Log;

/**
 * Internal — FastAPI / Hatchet → Laravel bridge for per-user inbox events.
 *
 * Service-key auth only. Dispatches
 * {@see App\Events\User\UserInboxUpdated} on the
 * `App.Models.User.{user_id}` private channel; the receiving Inbox page
 * triggers a router.reload, and the nav-bar inbox badge can increment
 * its counter from the count_delta field.
 *
 * Kinds (matches the three Inbox sources in InboxController):
 *   - mention   — silver.collaboration_mentions insert
 *   - review    — silver.collaboration_review_requests insert (status='pending')
 *   - refusal   — audit.query_audit_log insert/update where response_text
 *                 ends up NULL (the streaming worker determined the query
 *                 was refused at terminal time)
 */
class UserInboxBridgeController extends Controller
{
    private const ALLOWED_KINDS = [
        UserInboxUpdated::KIND_MENTION,
        UserInboxUpdated::KIND_REVIEW,
        UserInboxUpdated::KIND_REFUSAL,
    ];

    public function broadcast(Request $request): JsonResponse
    {
        $payload = $request->validate([
            'user_id' => ['required', 'integer', 'min:1'],
            'kind' => ['required', 'string', 'in:'.implode(',', self::ALLOWED_KINDS)],
            'count_delta' => ['nullable', 'integer', 'min:1', 'max:1000'],
            'payload' => ['nullable', 'array'],
        ]);

        UserInboxUpdated::dispatch(
            (int) $payload['user_id'],
            $payload['kind'],
            (int) ($payload['count_delta'] ?? 1),
            $payload['payload'] ?? [],
        );

        Log::info('user.inbox_updated.broadcast', [
            'user_id' => $payload['user_id'],
            'kind' => $payload['kind'],
            'count_delta' => $payload['count_delta'] ?? 1,
        ]);

        return response()->json(['ok' => true]);
    }
}
