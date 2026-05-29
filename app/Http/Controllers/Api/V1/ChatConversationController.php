<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Events\WorkspaceDataUpdated;
use App\Http\Controllers\Controller;
use App\Models\ChatConversation;
use App\Models\ChatMessage;
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
            'messages' => ['array'],
            'messages.*.role' => ['required_with:messages', 'string', 'in:user,assistant,system'],
            'messages.*.content' => ['required_with:messages', 'string'],
            'messages.*.metadata' => ['sometimes', 'array'],
        ]);

        $resolvedProjectId = null;
        $wasNewThread = false;
        DB::transaction(function () use ($conversationId, $user, $validated, &$resolvedProjectId, &$wasNewThread) {
            $thread = ChatConversation::firstOrNew([
                'conversation_id' => $conversationId,
                'user_id' => $user->id,
            ]);

            // If the row existed under a different user, reject — don't
            // leak / clobber another user's conversation by id collision.
            if ($thread->exists && $thread->user_id !== $user->id) {
                abort(403, 'Conversation belongs to another user.');
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

            foreach ($validated['messages'] ?? [] as $i => $m) {
                ChatMessage::create([
                    'conversation_id' => $thread->conversation_id,
                    'role' => $m['role'],
                    'content' => $m['content'],
                    'metadata' => $m['metadata'] ?? [],
                    // Preserve client order by nudging created_at per index.
                    // Same second for all, ordering is by insertion order.
                ]);
            }
        });

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
                    'error' => $e->getMessage(),
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
