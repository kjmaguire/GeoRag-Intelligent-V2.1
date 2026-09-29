<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Events\WorkspaceDataUpdated;
use App\Http\Controllers\Controller;
use App\Models\ChatConversation;
use App\Models\ChatMessage;
use App\Support\SafeErrorMessage;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;

/**
 * Server-side chat-history API.
 *
 * The React client treats localStorage as the fast path (instant reads,
 * offline resilience) and this API as the durable sync layer. Contract:
 *
 *   GET  /api/v1/conversations                  — list threads for the user
 *   GET  /api/v1/conversations/{id}             — fetch one thread + messages
 *   PUT  /api/v1/conversations/{id}             — upsert thread + messages (full replace)
 *   DELETE /api/v1/conversations/{id}           — delete thread
 *
 * Upsert is "full replace" — the client sends the authoritative thread
 * state, the server truncates and re-inserts messages inside a single
 * transaction. This keeps the client code trivial (no delta-sync); at the
 * message volumes we care about (≤50 per thread), re-insertion is fine.
 *
 * Auth: every endpoint checks that the conversation belongs to the
 * authenticated user. A user can never see another user's conversations.
 */
class ChatConversationController extends Controller
{
    /** Upper bound on messages in one full-replace sync (LAR-18). */
    private const MAX_MESSAGES = 500;

    /** Upper bound on one message's text, in characters (LAR-18). */
    private const MAX_CONTENT_CHARS = 100_000;

    public function index(Request $request): JsonResponse
    {
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        $threads = ChatConversation::where('user_id', $user->id)
            ->orderByDesc('updated_at')
            ->limit(100)
            ->get(['conversation_id', 'title', 'project_id', 'created_at', 'updated_at']);

        return response()->json([
            'conversations' => $threads->map(fn ($t) => [
                'id' => $t->conversation_id,
                'title' => $t->title,
                'project_id' => $t->project_id,
                'created_at' => $t->created_at?->toIso8601String(),
                'updated_at' => $t->updated_at?->toIso8601String(),
            ]),
        ]);
    }

    public function show(string $conversationId, Request $request): JsonResponse
    {
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        $thread = ChatConversation::with('messages')
            ->where('conversation_id', $conversationId)
            ->where('user_id', $user->id)
            ->first();

        if (! $thread) {
            return response()->json(['error' => 'not_found'], 404);
        }

        return response()->json([
            'id' => $thread->conversation_id,
            'title' => $thread->title,
            'project_id' => $thread->project_id,
            'created_at' => $thread->created_at?->toIso8601String(),
            'updated_at' => $thread->updated_at?->toIso8601String(),
            'messages' => $thread->messages->map(fn ($m) => [
                'id' => $m->message_id,
                'role' => $m->role,
                'content' => $m->content,
                'metadata' => $m->metadata ?? [],
                'created_at' => $m->created_at?->toIso8601String(),
            ]),
        ]);
    }

    /**
     * Full-replace upsert. Accepts the client's authoritative thread state
     * and syncs it to the DB inside a transaction.
     *
     * Body:
     *   {
     *     "title": "...",
     *     "project_id": "uuid|null",
     *     "messages": [
     *       { "role": "user"|"assistant"|"system",
     *         "content": "...",
     *         "metadata": {...} }, ...
     *     ]
     *   }
     */
    public function upsert(string $conversationId, Request $request): JsonResponse
    {
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        $validated = $request->validate([
            'title' => ['nullable', 'string', 'max:255'],
            'project_id' => ['nullable', 'uuid'],
            // Bounded (LAR-18): the sync is a full replace, so an unbounded
            // body is an unbounded delete + insert inside one transaction.
            'messages' => ['array', 'max:'.self::MAX_MESSAGES],
            'messages.*.role' => ['required_with:messages', 'string', 'in:user,assistant,system'],
            // `present|nullable`, not `required` (CHAT-4). An assistant turn
            // that failed before any text streamed has content ''; the
            // ConvertEmptyStringsToNull middleware makes that null, and a
            // `required` rule then 422'd EVERY later sync of the thread --
            // nothing after the first empty failure was ever persisted.
            'messages.*.content' => ['present', 'nullable', 'string', 'max:'.self::MAX_CONTENT_CHARS],
            'messages.*.metadata' => ['sometimes', 'nullable', 'array'],
        ]);

        // Tenancy gate — a client-supplied project_id was previously
        // persisted with no ownership check, letting an attacker attach
        // their own conversation to a victim workspace's project_id and
        // trigger a WorkspaceActivityBroadcast on that workspace's
        // activity channel below (a cross-tenant signal leak, even though
        // the broadcast channel itself is separately membership-gated).
        if (! empty($validated['project_id']) && ! $user->hasProjectAccess($validated['project_id'])) {
            return response()->json(['error' => 'project_not_found'], 404);
        }

        $incomingMessages = $validated['messages'] ?? [];

        $resolvedProjectId = null;
        $wasNewThread = false;
        $refusedToEmpty = false;
        DB::transaction(function () use ($conversationId, $user, $validated, $incomingMessages, &$resolvedProjectId, &$wasNewThread, &$refusedToEmpty) {
            // Look the id up WITHOUT the user filter (LAR-18). The previous
            // firstOrNew(['conversation_id' => X, 'user_id' => me]) could
            // never return another user's row, so the 403 below was
            // unreachable and a colliding id fell through to an INSERT that
            // hit the primary key and 500'd.
            $thread = ChatConversation::query()
                ->where('conversation_id', $conversationId)
                ->lockForUpdate()
                ->first();

            // If the row exists under a different user, reject — don't
            // leak / clobber another user's conversation by id collision.
            if ($thread !== null && (int) $thread->user_id !== (int) $user->id) {
                abort(403, 'Conversation belongs to another user.');
            }

            // CHAT-3. A sync that would empty a thread that has messages is
            // never a legitimate chat-page write (deleting a thread is
            // DELETE). It is what the page sent when "+ New" was clicked
            // mid-answer: the late `completed` handler persisted the NEW
            // (empty) transcript under the OLD thread id, and this
            // full-replace erased the whole previous conversation.
            if ($thread !== null && $incomingMessages === []
                && ChatMessage::where('conversation_id', $thread->conversation_id)->exists()) {
                $refusedToEmpty = true;

                return;
            }

            if ($thread === null) {
                $thread = new ChatConversation;
                $thread->conversation_id = $conversationId;
            }

            $wasNewThread = ! $thread->exists;
            $thread->user_id = $user->id;
            $thread->title = $validated['title'] ?? ($thread->title ?? 'New conversation');
            $thread->project_id = $validated['project_id'] ?? $thread->project_id;
            $thread->save();
            $resolvedProjectId = $thread->project_id;

            // Full-replace message sync. Safe because messages have no
            // foreign keys pointing at them and the UUID primary keys are
            // regenerated on each sync (the client doesn't need stable
            // server-side ids — localStorage keeps its own).
            ChatMessage::where('conversation_id', $thread->conversation_id)->delete();

            foreach (array_values($incomingMessages) as $i => $m) {
                ChatMessage::create([
                    'conversation_id' => $thread->conversation_id,
                    'role' => $m['role'],
                    // content is NOT NULL in the table; an empty failed
                    // assistant turn is stored as '' (CHAT-4).
                    'content' => (string) ($m['content'] ?? ''),
                    'metadata' => $m['metadata'] ?? [],
                    // The client's order, explicitly (CHAT-2 / LAR-5). Every
                    // row of this sync gets the same created_at second, and
                    // Postgres does not return ties in insertion order.
                    'position' => $i,
                ]);
            }
        });

        if ($refusedToEmpty) {
            return response()->json([
                'error' => 'refusing_to_empty_thread',
                'message' => 'A sync may not remove every message from an existing thread. Use DELETE to remove the thread.',
            ], 409);
        }

        // Phase 3 — broadcast WorkspaceDataUpdated with affected_types=['investigations']
        // so Foundry/Investigations refetches the conversation list. Only fires
        // when we have a project_id (page is project-scoped); skips otherwise
        // (a conversation with no project doesn't surface on the page). Best-effort.
        if ($resolvedProjectId !== null) {
            try {
                $workspaceId = DB::table('silver.projects')
                    ->where('project_id', $resolvedProjectId)
                    ->value('workspace_id');
                if ($workspaceId !== null) {
                    WorkspaceDataUpdated::dispatch(
                        (string) $workspaceId,
                        $resolvedProjectId,
                        $conversationId,
                        ['investigations'],
                    );
                }
            } catch (\Throwable $e) {
                Log::warning('ChatConversation: investigations broadcast failed', [
                    'conversation_id' => $conversationId,
                    'error' => SafeErrorMessage::forResponse($e),
                ]);
            }
        }

        return response()->json(['status' => 'synced', 'id' => $conversationId]);
    }

    public function destroy(string $conversationId, Request $request): JsonResponse
    {
        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        $count = ChatConversation::where('conversation_id', $conversationId)
            ->where('user_id', $user->id)
            ->delete();

        if ($count === 0) {
            return response()->json(['error' => 'not_found'], 404);
        }

        return response()->json(['status' => 'deleted', 'id' => $conversationId]);
    }
}
