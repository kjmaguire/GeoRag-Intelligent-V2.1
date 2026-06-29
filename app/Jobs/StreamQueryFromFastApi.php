<?php

declare(strict_types=1);

namespace App\Jobs;

use App\Events\QueryStreamEvent;
use App\Models\ChatMessage;
use App\Models\QueryAuditLog;
use App\Services\FastApiJwtMinter;
use Illuminate\Bus\Queueable;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Bus\Dispatchable;
use Illuminate\Queue\InteractsWithQueue;
use Illuminate\Queue\SerializesModels;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;
use Psr\Http\Message\StreamInterface;

/**
 * Proxies a natural-language query to FastAPI's /internal/queries endpoint,
 * reads the Server-Sent Events stream line-by-line, and re-broadcasts every
 * delta event over Reverb so the React frontend can receive it via Echo.
 *
 * Queue placement: this job runs on the dedicated "llm" queue (see A3 fix)
 * under its own Horizon supervisor so long-running streams never saturate
 * the default pool and starve other queued work.
 *
 * FastAPI SSE event vocabulary (see src/fastapi/app/routers/queries.py):
 *   - status    : progress message (e.g. "Analyzing query…")
 *   - delta     : a token chunk — re-broadcast as-is
 *   - citation  : a single citation's payload
 *   - completed : terminal success; carries the full GeoRAGResponse.
 *                 Triggers the audit-log completion write (response_text,
 *                 citations, sources_used, confidence, response_time_ms).
 *   - failed    : terminal error (timeout, validation, upstream LLM, etc.);
 *                 carries {error, code}. Triggers the audit-log failure
 *                 write (response_text receives an "[error: ...]" marker
 *                 because no dedicated failure columns exist today, and
 *                 response_time_ms records elapsed time for latency metrics
 *                 even on the failure path).
 *
 * Broadcasting channel name is "query.{queryId}" — a private channel.
 * The React client subscribes via Echo.private('query.{queryId}').
 */
class StreamQueryFromFastApi implements ShouldQueue
{
    use Dispatchable;
    use InteractsWithQueue;
    use Queueable;
    use SerializesModels;

    /**
     * Maximum seconds this job is allowed to run before Horizon kills it.
     * Generous enough for large RAG responses but bounded so stuck jobs
     * do not consume a worker forever.
     */
    public int $timeout = 300;

    /**
     * Do not automatically retry — a streaming session is stateful; replaying
     * it would produce duplicate events on the channel.
     */
    public int $tries = 1;

    public function __construct(
        private readonly string $queryId,
        private readonly string $projectId,
        private readonly string $queryText,
        private readonly string $channel,
        /**
         * Phase 3 / Step 3.2 — optional 12-field ContextEnvelope from the
         * query-builder UI, validated in QueryController::start(). NULL for
         * legacy callers that don't send the envelope (the FastAPI side
         * treats this as fully unspecified per Phase 2.4).
         *
         * @var array<string, mixed>|null
         */
        private readonly ?array $contextEnvelope = null,
        /**
         * Plan §3e — conversation_id used to load prior chat_messages
         * turns for multi-turn pronoun / demonstrative / comparative
         * resolution. NULL when the query isn't part of a chat thread
         * (legacy callers, single-shot queries from the Investigations
         * page). When set, the job loads the last N turns from
         * chat_messages + forwards them on the FastAPI payload.
         */
        private readonly ?string $conversationId = null,
    ) {
        // Dedicated queue (A3) so concurrent 270s LLM streams don't saturate
        // the default pool. Worker concurrency tuned on supervisor-llm in
        // config/horizon.php. Assigned in the constructor rather than via a
        // typed property default because PHP 8.2+ rejects child/trait
        // `$queue` composition when defaults differ.
        $this->queue = 'llm';
    }

    public function handle(): void
    {
        $this->startTime = microtime(true);
        $fastApiUrl = sprintf('%s/internal/queries', rtrim(config('services.fastapi.internal_url'), '/'));
        $serviceKey = config('services.fastapi.service_key');
        $streamTimeout = (int) config('services.fastapi.stream_timeout', 270);

        // B7 — mint a short-TTL JWT that carries the acting user identity so
        // FastAPI can enforce document-level RBAC. The user_id isn't on the
        // job constructor (queries are dispatched from QueryController without
        // it), so look it up on the audit row we're about to finalise. The
        // static X-Service-Key header stays for one release for a graceful
        // rollout; FastAPI will flip to JWT-required in a follow-up change.
        //
        // Defensive: a missing audit row OR a briefly-unavailable DB must not
        // crash the stream. Fall back to user_id='unknown' and let FastAPI
        // treat the request as unscoped (it already does under graceful
        // rollout). This also makes unit tests that exercise the stream
        // plumbing independent of DB fixtures.
        try {
            $auditRow = QueryAuditLog::where('query_id', $this->queryId)->first();
        } catch (\Throwable $e) {
            Log::warning('StreamQueryFromFastApi: audit row lookup failed (continuing with unknown user)', [
                'query_id' => $this->queryId,
                'exception' => $e->getMessage(),
            ]);
            $auditRow = null;
        }
        $userId = $auditRow?->user_id ?? 'unknown';
        // Audit 2026-06-27: carry workspace_id in the JWT so the FastAPI
        // lifecycle/RLS guard on the MAIN query path is actually enforced.
        // Previously this mint omitted workspace_id, so the guard was silently
        // skipped on every chat/RAG query. Derived from the project row.
        $workspaceId = DB::table('silver.projects')
            ->where('project_id', $this->projectId)
            ->value('workspace_id');
        $jwt = app(FastApiJwtMinter::class)->mint(
            $userId,
            $this->projectId,
            [], // roles — no role system yet (see B7 follow-up)
            $workspaceId !== null ? (string) $workspaceId : null,
        );

        // Note: the previous 600 ms subscription-race guard is no longer
        // needed — the QueryController now uses a two-phase handshake
        // (POST /queries, subscribe, POST /queries/{id}/start) so by the
        // time this job runs the client is guaranteed to be on the
        // broadcast channel.

        Log::info('StreamQueryFromFastApi: starting', [
            'query_id' => $this->queryId,
            'project_id' => $this->projectId,
            'channel' => $this->channel,
            'fastapi_url' => $fastApiUrl,
        ]);

        try {
            // Use native fopen with HTTP context for true blocking reads.
            // Guzzle's stream option returns eof()=true immediately when no
            // data is buffered, which causes us to miss the actual stream.
            $payloadData = [
                'query_id' => $this->queryId,
                'project_id' => $this->projectId,
                'query' => $this->queryText,
            ];
            // Phase 3 / Step 3.2 — forward the context envelope when the
            // UI supplied one. FastAPI's QueryRequest accepts this as an
            // optional dict; missing/null means "fully unspecified".
            if ($this->contextEnvelope !== null) {
                $payloadData['context_envelope'] = $this->contextEnvelope;
            }
            // Plan §3e — load prior chat history for the conversation
            // and forward as the FastAPI QueryRequest.history field.
            // The FastAPI side dispatches to the resolve_node which
            // expands pronouns / demonstratives against the history.
            // No-op when conversation_id is null or the conversation
            // has no prior turns (single-shot query).
            $history = $this->loadConversationHistory();
            if ($history !== []) {
                $payloadData['history'] = $history;
            }
            $payload = json_encode($payloadData);

            $context = stream_context_create([
                'http' => [
                    'method' => 'POST',
                    'header' => [
                        'Content-Type: application/json',
                        'Accept: text/event-stream',
                        'Authorization: Bearer '.$jwt,
                        // Kept for one release to give FastAPI a graceful
                        // cutover window; B7 follow-up drops it on that side.
                        'X-Service-Key: '.$serviceKey,
                    ],
                    'content' => $payload,
                    'timeout' => $streamTimeout,
                    'ignore_errors' => true,
                ],
            ]);

            // openHttpStream returns [resource, headers] — see method comment
            // for the $http_response_header scoping bug it works around.
            [$stream, $rawHeaders] = $this->openHttpStream($fastApiUrl, $context);
            if ($stream === false) {
                throw new \RuntimeException("Failed to open stream to FastAPI: {$fastApiUrl}");
            }

            // Extract status from response headers. responseHeaders() wraps
            // the header list so tests can inject a fake one. R2.
            $headers = $this->responseHeaders($rawHeaders);
            $statusLine = $headers[0] ?? '';
            preg_match('#HTTP/\S+\s+(\d+)#', $statusLine, $matches);
            $statusCode = isset($matches[1]) ? (int) $matches[1] : 0;

            Log::info('StreamQueryFromFastApi: got response', [
                'query_id' => $this->queryId,
                'status' => $statusCode,
            ]);

            if ($statusCode < 200 || $statusCode >= 300) {
                $body = stream_get_contents($stream);
                fclose($stream);
                $this->broadcastError(
                    "FastAPI returned HTTP {$statusCode}: ".substr($body, 0, 200),
                    $statusCode,
                );

                return;
            }

            // Read the SSE stream line by line. fgets() blocks waiting for data.
            $eventType = null;
            $dataBuffer = '';
            $eventCount = 0;

            while (! feof($stream)) {
                $line = fgets($stream);
                if ($line === false) {
                    break;
                }

                $line = rtrim($line, "\r\n");

                if ($line === '') {
                    if ($dataBuffer !== '') {
                        $this->dispatchSseEvent($eventType ?? 'delta', $dataBuffer);
                        $eventCount++;
                        $eventType = null;
                        $dataBuffer = '';
                    }

                    continue;
                }

                if (str_starts_with($line, 'event:')) {
                    $eventType = trim(substr($line, 6));
                } elseif (str_starts_with($line, 'data:')) {
                    $dataBuffer .= trim(substr($line, 5));
                }
            }

            if ($dataBuffer !== '') {
                $this->dispatchSseEvent($eventType ?? 'delta', $dataBuffer);
                $eventCount++;
            }

            fclose($stream);

            Log::info('StreamQueryFromFastApi: stream complete', [
                'query_id' => $this->queryId,
                'events_dispatched' => $eventCount,
            ]);

            // ── Audit log: update with response data ─────────────────────
            // On `completed`: write the full success payload.
            // On `failed`   : write an error marker into response_text (no
            //                 dedicated error column exists) plus the elapsed
            //                 time so latency metrics still reflect the run.
            //
            // We MUST go through the model (not a mass Query Builder update)
            // so the `encrypted` cast on response_text fires — otherwise
            // plaintext would be written straight to the DB column and the
            // A4 PII-at-rest guarantee would break on every completion.
            $elapsed = (int) ((microtime(true) - $this->startTime) * 1000);
            // Match the defensive lookup at the top of handle() — if the
            // audit row can't be fetched, skip the finalisation block
            // entirely so the stream itself still delivers events to the
            // client.
            try {
                $row = QueryAuditLog::where('query_id', $this->queryId)->first();
            } catch (\Throwable $e) {
                Log::debug('StreamQueryFromFastApi: audit row finalisation skipped (lookup failed)', [
                    'query_id' => $this->queryId,
                    'exception' => $e->getMessage(),
                ]);
                $row = null;
            }

            if ($row !== null && $this->completedPayload !== null) {
                $sourcesUsed = $this->completedPayload['sources_used'] ?? [];
                // R10 — attach routing decision alongside the flat source
                // list. Stored as a trailing marker so existing reads that
                // treat sources_used as a plain list keep working; we
                // prefix with a sentinel so downstream consumers can split.
                // Also mirror into a top-level `llm_model` refresh so the
                // analytics aggregation (top_queries, avg_latency_ms by
                // model) reflects the actual model that served, not the
                // initial dispatch-time default.
                if ($this->routingPayload !== null) {
                    $sourcesUsed = array_merge(
                        is_array($sourcesUsed) ? $sourcesUsed : [],
                        ['__routing__:'.json_encode($this->routingPayload)],
                    );
                    if (! empty($this->routingPayload['model'])) {
                        $row->llm_model = $this->routingPayload['model'];
                    }
                }
                $row->response_text = $this->completedPayload['text'] ?? null;
                $row->citations = $this->completedPayload['citations'] ?? [];
                $row->sources_used = $sourcesUsed;
                $row->confidence = $this->completedPayload['confidence'] ?? null;
                $row->response_time_ms = $elapsed;

                // Plan §4b — persist typed guard codes from
                // GeoRAGResponse.guard_error_codes into chat_messages.metadata
                // so historical messages can re-render the guard surface
                // (RefusalBanner / AmbiguityPicker / ConflictSideBySide /
                // PartialAnswerCard / IncidentReportBanner) when a thread
                // is re-opened. The live SSE broadcast already forwards
                // these to React in real-time via QueryStreamEvent — this
                // block adds durability across sessions.
                $guardCodes = $this->completedPayload['guard_error_codes'] ?? null;
                if (is_array($guardCodes) && $guardCodes !== []) {
                    $existing = is_array($row->metadata) ? $row->metadata : [];
                    $existing['guard_error_codes'] = array_values(array_filter(
                        $guardCodes,
                        fn ($c) => is_string($c) && $c !== '',
                    ));
                    $row->metadata = $existing;
                }

                $row->save();
            } elseif ($row !== null && $this->failedPayload !== null) {
                // On failure still record the routing decision so we can
                // distinguish "DEEP tier failed" from "FAST tier failed"
                // in post-incident analytics.
                if ($this->routingPayload !== null) {
                    $row->sources_used = array_merge(
                        is_array($row->sources_used) ? $row->sources_used : [],
                        ['__routing__:'.json_encode($this->routingPayload)],
                    );
                    if (! empty($this->routingPayload['model'])) {
                        $row->llm_model = $this->routingPayload['model'];
                    }
                }
                $row->response_text = $this->formatFailureMarker($this->failedPayload);
                $row->response_time_ms = $elapsed;
                $row->save();
            }
        } catch (\Throwable $e) {
            Log::error('StreamQueryFromFastApi failed', [
                'query_id' => $this->queryId,
                'exception' => $e->getMessage(),
            ]);

            $elapsed = (int) ((microtime(true) - $this->startTime) * 1000);
            $row = QueryAuditLog::where('query_id', $this->queryId)->first();
            if ($row !== null) {
                $row->response_text = '[error: '.substr($e->getMessage(), 0, 480).']';
                $row->response_time_ms = $elapsed;
                $row->save();
            }

            $this->broadcastError($e->getMessage(), 500);

            throw $e;
        }
    }

    /**
     * Terminal-failure hook (C8).
     *
     * Invoked by Horizon when the job exhausts its attempts OR is SIGTERM'd
     * at the `$timeout` boundary mid-stream. In the SIGTERM case the catch
     * block in handle() does NOT run (the worker dies inside fgets()), so
     * without this method the audit row and the frontend are both left in
     * limbo: the browser waits for its 5 min client timeout and the audit
     * row never gets a failure marker.
     *
     * Two important differences from the in-handle catch:
     *   1. Runs on a FRESH deserialised instance — the captured $startTime
     *      is 0. We derive elapsed time from the audit row's dispatched_at
     *      instead so the failure-path latency metric is still meaningful.
     *   2. Broadcasts `event: 'failed'` (not `'error'`), matching the
     *      FastAPI SSE vocabulary + the frontend's terminal handler.
     *
     * Idempotent: if the row is already marked failed, a repeat invocation
     * is a no-op on the audit side (the broadcast still fires in case the
     * first one was lost — the frontend already tolerates a duplicate
     * terminal event).
     */
    public function failed(\Throwable $e): void
    {
        Log::error('StreamQueryFromFastApi: terminal failure', [
            'query_id' => $this->queryId,
            'exception' => get_class($e),
            'message' => $e->getMessage(),
        ]);

        $row = QueryAuditLog::where('query_id', $this->queryId)->first();

        if ($row === null) {
            Log::warning('StreamQueryFromFastApi::failed: audit row not found', [
                'query_id' => $this->queryId,
            ]);
        } else {
            $alreadyMarked = is_string($row->response_text)
                && str_starts_with($row->response_text, '[FAILED');

            if (! $alreadyMarked) {
                // Elapsed since dispatched_at (set by QueryController::start).
                // Falls back to 0 if for any reason dispatched_at wasn't
                // populated — better an unknown latency than throwing here.
                $elapsed = 0;
                if ($row->dispatched_at !== null) {
                    $elapsed = (int) abs(now()->diffInMilliseconds($row->dispatched_at));
                }

                $row->response_text = '[FAILED: '.get_class($e).' — '
                                       .substr($e->getMessage(), 0, 480).']';
                $row->response_time_ms = $elapsed;
                $row->save();
            }
        }

        // Broadcast a terminal `failed` event so the client stops waiting.
        // Deliberately NOT piggy-backing broadcastError() (which emits
        // event:'error') — `failed` is the terminal vocabulary in the
        // FastAPI SSE contract and the frontend routes it to its terminal
        // error UI via the Retry affordance (A5).
        //
        // Defensive: if the broadcast itself fails (Reverb down, payload
        // limit, network blip) we MUST NOT let the failed() handler die —
        // the audit row has already been written above. Swallow + log.
        // The frontend's own timeout watchdog (P0.2) will catch the silent
        // case where no terminal event ever reaches the client.
        try {
            broadcast(new QueryStreamEvent(
                $this->channel,
                'failed',
                [
                    'event' => 'failed',
                    'query_id' => $this->queryId,
                    'code' => 'JOB_FAILED',
                    'error' => 'Your query could not be completed. Please try again.',
                ],
            ));
        } catch (\Throwable $broadcastError) {
            Log::error('StreamQueryFromFastApi::failed: terminal broadcast failed', [
                'query_id' => $this->queryId,
                'broadcast_exception' => $broadcastError->getMessage(),
            ]);
        }
    }

    /** Completed event payload — captured for audit log update. */
    private ?array $completedPayload = null;

    /** Failed event payload — captured for audit log failure update. */
    private ?array $failedPayload = null;

    /**
     * R10 — routing decision captured from FastAPI's per-query `routing`
     * frame ({tier, model, reason}). Flushed into the audit row on
     * completion or failure so analytics can report tier/model mix.
     * Latest routing decision wins if FastAPI emits multiple (e.g. the
     * failover path emits a second one with reason='failover').
     */
    private ?array $routingPayload = null;

    private function formatFailureMarker(array $payload): string
    {
        $code = $payload['code'] ?? 'UNKNOWN';
        $error = $payload['error'] ?? $payload['message'] ?? 'unspecified';

        return '[error: '.$code.' — '.substr((string) $error, 0, 480).']';
    }

    private float $startTime = 0;

    /**
     * Parse and broadcast a single SSE event.
     *
     * FastAPI event vocabulary (authoritative — see class docblock):
     *   status | delta | citation | completed | failed
     *
     * `completed` and `failed` are terminal; they drive the audit-log
     * finalisation in handle() after the stream drains.
     */
    private function dispatchSseEvent(string $eventType, string $rawData): void
    {
        $payload = json_decode($rawData, true);

        // If FastAPI sends a plain string delta rather than JSON, wrap it.
        if ($payload === null) {
            $payload = ['text' => $rawData];
        }

        $payload['event'] = $eventType;
        $payload['query_id'] = $this->queryId;

        // Capture terminal payloads for audit logging after the stream ends.
        if ($eventType === 'completed') {
            $this->completedPayload = $payload;
        } elseif ($eventType === 'failed') {
            $this->failedPayload = $payload;
        } elseif ($eventType === 'routing') {
            // B1 follow-up (R10) — FastAPI emits a routing frame per query
            // carrying {tier, model, reason}. Persist to the audit row so
            // the Query Usage analytics panel can report cost/model mix and
            // so operators can see which queries hit DEEP tier vs failed
            // over from FAST. We capture on the instance first and flush
            // in handle() after the stream drains so a partial stream
            // doesn't leave a half-written row.
            $this->routingPayload = [
                'tier' => $payload['tier'] ?? null,
                'model' => $payload['model'] ?? null,
                'reason' => $payload['reason'] ?? 'classifier',
            ];
        }

        // Defensive: a single SSE frame failing to broadcast (size limit,
        // transient Reverb hiccup) MUST NOT abort the stream — we still
        // want to capture the terminal completed/failed payload for the
        // audit row, and subsequent frames may broadcast fine. Swallow
        // here and let handle() complete normally. The frontend's
        // timeout watchdog catches the rare case where every frame fails.
        try {
            broadcast(new QueryStreamEvent($this->channel, $eventType, $payload));
        } catch (\Throwable $broadcastError) {
            Log::warning('StreamQueryFromFastApi: SSE frame broadcast failed', [
                'query_id' => $this->queryId,
                'event_type' => $eventType,
                'broadcast_exception' => $broadcastError->getMessage(),
            ]);
        }
    }

    /**
     * Broadcast an error event so the frontend can surface a meaningful message
     * rather than silently timing out.
     */
    private function broadcastError(string $message, int $code): void
    {
        // Defensive: a broken broadcast layer must not propagate back into
        // the caller's catch block — handle() relies on broadcastError()
        // running to completion so it can return cleanly. If Reverb is
        // unreachable, the frontend's timeout watchdog (P0.2) will fire
        // its own terminal error UI.
        try {
            broadcast(new QueryStreamEvent(
                $this->channel,
                'error',
                [
                    'event' => 'error',
                    'query_id' => $this->queryId,
                    'code' => $code,
                    'message' => $message,
                ],
            ));
        } catch (\Throwable $broadcastError) {
            Log::error('StreamQueryFromFastApi::broadcastError: broadcast failed', [
                'query_id' => $this->queryId,
                'original_code' => $code,
                'original_message' => $message,
                'broadcast_exception' => $broadcastError->getMessage(),
            ]);
        }
    }

    /**
     * R2 — overridable seam for the native fopen() call. Production
     * behaviour is unchanged; unit tests subclass this job and return a
     * php://memory or data:// stream pre-populated with a canned SSE
     * body. This replaces the old Http::fake() pattern, which silently
     * did nothing because the job uses native fopen(), not Laravel's
     * HTTP client.
     *
     * Returns a tuple of [resource|false, array<int,string>]. The headers
     * MUST come back alongside the stream because PHP populates
     * $http_response_header as a magic variable in the SAME function
     * scope where fopen() runs — calling fopen from a helper and then
     * reading $http_response_header in the caller produces nothing.
     * That bug used to cause every job to log "got response status=0"
     * and exit via broadcastError without ever consuming the body.
     *
     * @param resource $context Result of stream_context_create()
     *
     * @return array{0: resource|false, 1: array<int, string>}
     */
    protected function openHttpStream(string $url, $context): array
    {
        $stream = @fopen($url, 'r', false, $context);
        // PHP magic: $http_response_header is set as a local variable in
        // this function's scope right after fopen returns. Capture it now
        // before it goes out of scope.
        $headers = $http_response_header ?? [];

        return [$stream, $headers];
    }

    /**
     * R2 — overridable accessor for the PHP magic variable
     * `$http_response_header`, which is populated as a side-effect of
     * fopen() on an HTTP stream. Tests override to return a fake header
     * list without needing a real HTTP round-trip.
     *
     * @param array<int, string>|null $magic The ambient $http_response_header
     *                                       from handle(); null when fopen
     *                                       didn't populate one.
     *
     * @return array<int, string>
     */
    protected function responseHeaders(?array $magic): array
    {
        return $magic ?? [];
    }

    /**
     * Read a single newline-terminated line from a PSR-7 stream body.
     * Reads byte-by-byte to avoid buffering entire chunks at once, which
     * would delay token delivery to the broadcast layer.
     */
    private function readLine(StreamInterface $body): string
    {
        $line = '';

        while (! $body->eof()) {
            $byte = $body->read(1);

            if ($byte === "\n") {
                break;
            }

            $line .= $byte;
        }

        // Strip trailing carriage return for CRLF line endings.
        return rtrim($line, "\r");
    }

    /**
     * Plan §3e — load up to the last N chat turns for the active
     * conversation and shape them for the FastAPI resolve_node.
     *
     * Returns an empty array when:
     *   - $this->conversationId is null (single-shot query)
     *   - The conversation has no prior turns
     *   - The DB lookup fails (logged + ignored — multi-turn is
     *     opt-in, never block the answer path)
     *
     * Output shape matches FastAPI QueryRequest.history per
     * docs/architecture/multi_turn_resolution_spec.md §6.1:
     *
     *   [
     *     {turn_index, role, text, entity_mentions: [...]},
     *     ...
     *   ]
     *
     * entity_mentions are read off chat_messages.metadata when the
     * upstream NER has populated them; the FastAPI resolve_node falls
     * back to heuristic extraction when empty.
     *
     * @return array<int, array<string, mixed>>
     */
    private const HISTORY_MAX_TURNS = 20;

    /**
     * @return array<int, array<string, mixed>>
     */
    private function loadConversationHistory(): array
    {
        if ($this->conversationId === null) {
            return [];
        }

        try {
            $messages = ChatMessage::query()
                ->where('conversation_id', $this->conversationId)
                ->orderBy('created_at')
                ->orderBy('id')
                ->limit(self::HISTORY_MAX_TURNS)
                ->get(['id', 'conversation_id', 'role', 'content', 'metadata']);
        } catch (\Throwable $e) {
            Log::warning('StreamQueryFromFastApi: chat history load failed', [
                'conversation_id' => $this->conversationId,
                'error' => $e->getMessage(),
            ]);

            return [];
        }

        $history = [];
        foreach ($messages as $i => $msg) {
            $mentions = [];
            $meta = is_array($msg->metadata) ? $msg->metadata : [];
            $rawMentions = $meta['entity_mentions'] ?? [];
            if (is_array($rawMentions)) {
                foreach ($rawMentions as $m) {
                    if (! is_array($m) || empty($m['surface_form'])) {
                        continue;
                    }
                    $mentions[] = [
                        'surface_form' => (string) $m['surface_form'],
                        'entity_type' => (string) ($m['entity_type'] ?? 'hole'),
                        'turn_index' => (int) $i,
                        'normalised_id' => $m['normalised_id'] ?? null,
                    ];
                }
            }

            $history[] = [
                'turn_index' => (int) $i,
                'role' => (string) ($msg->role ?? 'user'),
                'text' => (string) ($msg->content ?? ''),
                'entity_mentions' => $mentions,
            ];
        }

        return $history;
    }
}
