<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Events\WorkspaceDataUpdated;
use App\Http\Controllers\Controller;
use App\Http\Middleware\InjectTraceparent;
use App\Http\Requests\StoreQueryRequest;
use App\Jobs\StreamQueryFromFastApi;
use App\Models\ChatConversation;
use App\Models\QueryAuditLog;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;

/**
 * RAG query entry point.
 *
 * Uses a two-phase handshake so the client can subscribe to the broadcast
 * channel before the Horizon streaming job fires its first event. Without
 * this, cached FastAPI responses finish dispatching before Echo.channel()
 * completes the WebSocket subscribe frame and the UI never receives the
 * `completed` event.
 *
 *   1. POST /api/v1/queries        — reserve a query_id + channel, persist
 *                                    the audit log row, RETURN WITHOUT
 *                                    DISPATCHING the job.
 *   2. (client) Echo.channel(channel).listen(...)
 *   3. POST /api/v1/queries/{id}/start — dispatch the Horizon job now that
 *                                    the subscription is guaranteed live.
 *
 * Idempotency: `start` is safe to call exactly once. A second call 409s so
 * a buggy client retry can't double-broadcast the same stream.
 */
class QueryController extends Controller
{
    /**
     * Phase 1 — reserve the query. No job dispatch.
     *
     * Authorization: the request must come from an authenticated user who
     * has explicit access to the target project via the project_user pivot.
     * StoreQueryRequest confirms the project EXISTS; here we additionally
     * confirm the user owns or is a member of it. Without this check any
     * authenticated user could fire RAG queries against any project_id
     * and siphon data out via the streaming response.
     */
    public function store(StoreQueryRequest $request): JsonResponse
    {
        $validated = $request->validated();
        $queryText = $validated['query'];
        $projectId = $validated['project_id'];

        $user = $request->user();
        if ($user === null || ! $user->hasProjectAccess($projectId)) {
            return response()->json([
                'error' => 'forbidden',
                'message' => 'You do not have access to the specified project.',
            ], 403);
        }

        $queryId = (string) Str::uuid();
        $channel = "query.{$queryId}";

        // ── Audit log: record the query before dispatching ──────────────
        // `response_text` is NULL here; the Horizon job fills it after
        // the streaming run completes. Absence of response_text + a NULL
        // `dispatched_at` is how we distinguish reserved-but-not-started
        // rows from dispatched ones in Phase 2 below.
        //
        // Phase 3 / Step 3.2 — the optional ContextEnvelope flows through
        // /queries/{id}/start (not here) to avoid a new audit column. The
        // store() validator still runs the envelope through its rules so
        // a malformed envelope is rejected before the query_id is reserved.
        QueryAuditLog::create([
            'user_id' => $request->user()?->id,
            'project_id' => $projectId,
            'query_id' => $queryId,
            'query_text' => $queryText,
            'ip_address' => $request->ip(),
            // Provisional. This is the model we EXPECT to answer with, not
            // the one that did — no model has been called yet at reservation
            // time. StreamQueryFromFastApi overwrites it from the completed
            // payload's `llm_model` once the answer lands. The literal
            // second argument here used to read 'qwen2.5:14b' — an Ollama
            // model retired two backends ago — which was unreachable dead
            // code (config() only falls back when the KEY is absent, and it
            // is not) but described the wrong system to anyone reading it.
            'llm_model' => config('services.fastapi.llm_model'),
        ]);

        // Phase 3 — broadcast WorkspaceDataUpdated with affected_types=['audit_log']
        // so Foundry/AuditLog + Foundry/ProjectAnalytics + Foundry/Overview
        // re-fetch when a new query lands. The receiving hook applies a 2s
        // trailing debounce so high-frequency query bursts collapse into one
        // partial reload. Best-effort — broadcast failure must not fail the
        // reservation (the QueryAuditLog row is already committed).
        if ($projectId !== null) {
            try {
                $workspaceId = DB::table('silver.projects')
                    ->where('project_id', $projectId)
                    ->value('workspace_id');
                if ($workspaceId !== null) {
                    WorkspaceDataUpdated::dispatch(
                        (string) $workspaceId,
                        $projectId,
                        $queryId,
                        ['audit_log'],
                    );
                }
            } catch (\Throwable $e) {
                Log::warning('QueryController: audit_log broadcast failed', [
                    'query_id' => $queryId,
                    'project_id' => $projectId,
                    'error' => $e->getMessage(),
                ]);
            }
        }

        return response()->json([
            'query_id' => $queryId,
            'channel' => $channel,
            'message' => 'Query reserved. Subscribe to the channel, then POST /queries/{query_id}/start to begin streaming.',
        ], 202);
    }

    /**
     * Phase 2 — dispatch the streaming job.
     *
     * Called by the client after it has subscribed to the Reverb channel.
     * Idempotent by row-level locking on the audit row — a concurrent
     * duplicate call sees `dispatched_at` already set and returns 409
     * without firing a second job.
     */
    public function start(string $queryId, Request $request): JsonResponse
    {
        $row = QueryAuditLog::where('query_id', $queryId)
            ->where('user_id', $request->user()?->id)
            ->first();

        if (! $row) {
            return response()->json([
                'error' => 'query_not_found',
                'message' => 'Unknown query_id, or it was reserved by a different user.',
            ], 404);
        }

        // Phase 3 / Step 3.2 — optional context envelope sent by the
        // query-builder UI on /start (not /store, to avoid an audit-log
        // column migration). Same field shape as StoreQueryRequest
        // validates — re-validate here so a tampered envelope cannot
        // flow through. Reuse only the envelope-prefixed rules; the
        // root-level `query`/`project_id` requirements belong to /store
        // and would falsely fail validation here.
        $envelope = $request->input('context_envelope');
        if ($envelope !== null) {
            $envelopeRules = ['context_envelope' => ['array']];
            foreach ((new StoreQueryRequest)->rules() as $key => $rule) {
                if (str_starts_with($key, 'context_envelope.')) {
                    $envelopeRules[$key] = $rule;
                }
            }
            $validator = validator(['context_envelope' => $envelope], $envelopeRules);
            if ($validator->fails()) {
                $messages = $validator->errors()->messages();
                $failedKey = array_key_first($messages) ?? 'context_envelope';
                $firstMsg = $validator->errors()->first() ?: 'Malformed envelope.';

                return response()->json([
                    'error' => 'invalid_context_envelope',
                    'message' => "{$failedKey}: {$firstMsg}",
                ], 422);
            }
        }

        // Plan §3e — optional conversation_id forwarded from the chat UI
        // for multi-turn resolution. NULL for single-shot queries (e.g.
        // the Investigations page) — the job is a no-op for history
        // loading in that case.
        //
        // Resolved BEFORE dispatched_at is stamped (LAR-13). It used to be
        // resolved after: a non-UUID id made the ownership query below
        // raise on Postgres (22P02), the request 500'd with the row already
        // stamped, and every retry got 409 already_dispatched — no job ever
        // ran and the chat sat on its watchdog. A malformed id is now
        // dropped like an unowned one.
        $conversationId = $request->input('conversation_id');
        if (! is_string($conversationId) || ! Str::isUuid($conversationId)) {
            $conversationId = null; // tolerate bad input; multi-turn is opt-in
        }
        // Audit 2026-06-27: IDOR guard. conversation_id is client-supplied and
        // chat_conversations are scoped to user_id (NOT shared). Without this
        // check a user could pass another user's conversation_id and exfil that
        // thread's history into the LLM prompt. Fail closed — drop an unowned
        // id back to single-shot rather than leaking.
        if ($conversationId !== null) {
            $ownsConversation = ChatConversation::query()
                ->where('conversation_id', $conversationId)
                ->where('user_id', $request->user()?->id)
                ->exists();
            if (! $ownsConversation) {
                Log::warning('QueryController: dropping unowned conversation_id', [
                    'query_id' => $queryId,
                    'user_id' => $request->user()?->id,
                ]);
                $conversationId = null;
            }
        }

        // Atomic check-and-set on dispatched_at under a row lock so two
        // racing `start` calls can't both dispatch. The audit table's
        // primary key is `audit_id` (UUID), not `id`.
        $dispatched = DB::transaction(function () use ($row) {
            $fresh = QueryAuditLog::where('audit_id', $row->audit_id)
                ->lockForUpdate()
                ->first();
            if ($fresh->dispatched_at !== null) {
                return false;  // already dispatched
            }
            $fresh->dispatched_at = now();
            $fresh->save();

            return true;
        });

        if (! $dispatched) {
            return response()->json([
                'error' => 'already_dispatched',
                'query_id' => $queryId,
            ], 409);
        }

        $channel = "query.{$queryId}";
        // Carry this request's W3C trace context into the job so the
        // FastAPI call it makes lands in the same trace. InjectTraceparent
        // put it on the request attributes; nothing had ever read it back
        // out, so the trace died at the queue boundary.
        try {
            StreamQueryFromFastApi::dispatch(
                $queryId,
                $row->project_id,
                $row->query_text,
                $channel,
                $envelope,         // Phase 3 / Step 3.2 — context envelope dict / null.
                $conversationId,   // Plan §3e — chat thread for multi-turn history.
            )->withTraceparent(
                $request->attributes->get(InjectTraceparent::ATTRIBUTE_KEY),
            );
        } catch (\Throwable $e) {
            // LAR-13 — the push failed (Redis blip), so no job exists. Undo
            // the stamp, or every retry would 409 against a query that was
            // never started.
            QueryAuditLog::where('audit_id', $row->audit_id)->update(['dispatched_at' => null]);
            Log::error('QueryController: dispatch failed, dispatched_at reset', [
                'query_id' => $queryId,
                'error' => $e->getMessage(),
            ]);

            return response()->json([
                'error' => 'dispatch_failed',
                'message' => 'The query could not be queued. Please try again.',
            ], 503);
        }

        return response()->json([
            'query_id' => $queryId,
            'channel' => $channel,
            'status' => 'dispatched',
        ], 202);
    }

    /**
     * The outcome of a query as the audit row records it (CHAT-8).
     *
     * The recovery path for a tab that lost the terminal frame: a pusher-js
     * reconnect during `completed` (Reverb rolling deploy, laptop sleep,
     * Wi-Fi switch), an oversized frame (CHAT-5), or the idle watchdog.
     * Before this, such a tab showed an unverified partial even though the
     * answer sat finalised in the audit row. The FastAPI replay buffer is
     * not usable for this — the browser only ever knows the query_id.
     *
     * Scoped exactly like the broadcast channel (routes/channels.php): the
     * row's owner, with live access to the row's project.
     */
    public function result(string $queryId, Request $request): JsonResponse
    {
        $user = $request->user();
        $row = $user === null ? null : QueryAuditLog::where('query_id', $queryId)
            ->where('user_id', $user->id)
            ->first();

        if ($row === null || ($row->project_id !== null && ! $user->hasProjectAccess($row->project_id))) {
            return response()->json(['error' => 'query_not_found'], 404);
        }

        $text = $row->response_text;
        if ($row->dispatched_at === null) {
            return response()->json(['query_id' => $queryId, 'status' => 'reserved']);
        }
        if ($text === null) {
            return response()->json(['query_id' => $queryId, 'status' => 'running']);
        }

        if (str_starts_with($text, '[error:') || str_starts_with($text, '[FAILED')) {
            // Only the machine code leaves the server; the marker's free
            // text can hold exception detail (CHAT-20).
            $code = preg_match('/^\[error: ([A-Z0-9_]+) —/u', $text, $m) === 1 ? $m[1] : 'JOB_FAILED';

            return response()->json([
                'query_id' => $queryId,
                'status' => 'failed',
                'code' => $code,
                'error' => 'The query did not complete. Please try again.',
            ]);
        }

        $metadata = is_array($row->metadata) ? $row->metadata : [];

        return response()->json([
            'query_id' => $queryId,
            'status' => 'completed',
            'text' => $text,
            'citations' => $row->citations ?? [],
            'confidence' => $row->confidence,
            'validation_state' => $metadata['validation_state'] ?? null,
            'answer_run_id' => $metadata['answer_run_id'] ?? null,
            'refusal_payload' => $metadata['refusal_payload'] ?? null,
            'guard_error_codes' => $metadata['guard_error_codes'] ?? [],
            'llm_model' => $row->llm_model,
        ]);
    }

    /**
     * Ask the streaming job to stop (CHAT-18).
     *
     * Stop in the chat used to only leave the channel; the Horizon job and
     * the FastAPI run carried on for up to 180 s, holding an llm slot and
     * billing the LLM. The job polls this flag between frames and closes
     * the stream, which cancels the FastAPI run via client disconnect.
     */
    public function cancel(string $queryId, Request $request): JsonResponse
    {
        $exists = QueryAuditLog::where('query_id', $queryId)
            ->where('user_id', $request->user()?->id)
            ->exists();

        if (! $exists) {
            return response()->json(['error' => 'query_not_found'], 404);
        }

        Cache::put(StreamQueryFromFastApi::cancelCacheKey($queryId), true, now()->addMinutes(10));

        return response()->json(['query_id' => $queryId, 'status' => 'cancel_requested'], 202);
    }
}
