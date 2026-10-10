<?php

declare(strict_types=1);

namespace App\Jobs;

use App\Events\QueryStreamEvent;
use App\Http\Middleware\InjectTraceparent;
use App\Models\ChatMessage;
use App\Models\QueryAuditLog;
use App\Services\FastApiJwtMinter;
use Illuminate\Bus\Queueable;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Bus\Dispatchable;
use Illuminate\Queue\InteractsWithQueue;
use Illuminate\Queue\SerializesModels;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Facades\Log;

/**
 * Proxies a natural-language query to FastAPI's /internal/queries endpoint,
 * reads the Server-Sent Events stream line-by-line, and re-broadcasts every
 * delta event over Reverb so the React frontend can receive it via Echo.
 *
 * Queue placement: this job runs on the dedicated "llm" queue (see A3 fix)
 * under its own Horizon supervisor so long-running streams never saturate
 * the default pool and starve other queued work.
 *
 * FastAPI SSE event vocabulary — the contract is these six names, declared
 * identically in src/fastapi/app/routers/queries.py and consumed by
 * resources/js/Pages/Foundry/Chat.tsx (tests/Unit/Jobs/SseVocabularyContractTest
 * fails if the three drift):
 *
 *   SSE vocabulary: status · bind · delta · citation · completed · failed
 *
 *   - status    : progress message (e.g. "Analyzing query…"). Also used,
 *                 with heartbeat=true, as FastAPI's periodic keep-alive
 *                 during silent phases so the browser's idle watchdog does
 *                 not fire while the model is thinking (CHAT-6).
 *   - bind      : the citation manifest, bound before the first token
 *   - delta     : a token chunk — re-broadcast as-is ({token, token_seq})
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
     * Headroom between the inner HTTP read timeout and this job's own
     * timeout, in seconds. Covers the post-stream work: the audit-log
     * completion write and the terminal broadcast.
     */
    private const TIMEOUT_HEADROOM_SECONDS = 30;

    /**
     * Maximum seconds this job is allowed to run before Horizon kills it.
     *
     * DERIVED from services.fastapi.stream_timeout in the constructor, not
     * configured independently. The invariant is that the inner Guzzle read
     * timeout must expire first, so the stream fails with a diagnosable
     * ConnectionException that failed() can turn into a terminal `failed`
     * event -- rather than Horizon killing the worker mid-stream, which
     * leaves the client waiting on its own watchdog.
     *
     * That invariant used to live in a comment in config/services.php
     * ("Must be less than the Horizon job $timeout (300 s)") next to an
     * env-tunable value, with the 300 hard-coded here. Raising
     * FASTAPI_STREAM_TIMEOUT past 300 would have inverted it silently.
     *
     * The property default stands in for jobs serialised before this became
     * dynamic; the constructor overwrites it for every new dispatch.
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

        $this->timeout = self::timeoutSeconds();
    }

    /**
     * The job timeout implied by the configured stream timeout.
     *
     * Public so a test can assert the ordering without reaching into a
     * dispatched job.
     */
    public static function timeoutSeconds(): int
    {
        return (int) config('services.fastapi.stream_timeout', 270)
            + self::TIMEOUT_HEADROOM_SECONDS;
    }

    /**
     * W3C trace id for the HTTP request that queued this job, so the
     * FastAPI call it makes joins the same trace.
     *
     * An ordinary property with an explicit default, not a promoted
     * constructor property: promoted defaults belong to the parameter,
     * not the property, so a job serialised before this field existed
     * would unserialise with the property UNINITIALISED and throw on
     * first read. A property-level default is applied at object
     * creation and then overwritten by whatever the payload carries,
     * so in-flight jobs drain safely across the deploy.
     */
    private ?string $traceparent = null;

    /**
     * Set the trace context this job should propagate. Called by the
     * dispatcher with `$request->attributes->get(InjectTraceparent::ATTRIBUTE_KEY)`.
     */
    public function withTraceparent(?string $traceparent): self
    {
        $this->traceparent = InjectTraceparent::isValid($traceparent)
            ? $traceparent
            : null;

        return $this;
    }

    /** The 32-hex trace-id, for log correlation. Null when unset. */
    public function traceId(): ?string
    {
        return InjectTraceparent::traceIdOf($this->traceparent);
    }

    public function handle(): void
    {
        $this->startTime = microtime(true);
        $fastApiUrl = sprintf('%s/internal/queries', rtrim(config('services.fastapi.internal_url'), '/'));
        $serviceKey = config('services.fastapi.service_key');
        $streamTimeout = (int) config('services.fastapi.stream_timeout', 270);

        // B7 — mint a short-TTL JWT that carries the acting user identity AND
        // the workspace so FastAPI can enforce document-level RBAC and the
        // workspace lifecycle/RLS guard on the main RAG path. The user_id isn't
        // on the job constructor (queries are dispatched from QueryController
        // without it), so it is read off the audit row we're about to finalise.
        //
        // Fail closed. This used to fall back to sub='unknown' / no
        // workspace_id ("continuing unscoped") when either lookup failed, which
        // ran the RAG pipeline WITHOUT the tenant guard -- a tenant-isolation
        // defect that only shows up when the DB blips. An unresolved identity
        // now ends the job with a terminal `failed` frame; FastAPI is never
        // called. The job does not throw: tries=1, and failed() would only
        // broadcast a second terminal.
        try {
            $auditRow = $this->lookupAuditRow();
        } catch (\Throwable $e) {
            Log::error('StreamQueryFromFastApi: audit row lookup failed — refusing to run unscoped', [
                'query_id' => $this->queryId,
                'exception' => $e->getMessage(),
            ]);
            $this->failBeforeStream('AUDIT_LOOKUP_FAILED', null);

            return;
        }

        if ($auditRow === null) {
            Log::error('StreamQueryFromFastApi: audit row not found — refusing to run unscoped', [
                'query_id' => $this->queryId,
            ]);
            $this->failBeforeStream('IDENTITY_UNRESOLVED', null);

            return;
        }

        // CHAT-14 — a job picked up long after /start streams to nobody:
        // the browser's idle watchdog has already given up, so calling
        // FastAPI only bills an LLM run no one will see. Happens when the
        // llm supervisor was down, backlogged, or restarted mid-queue.
        if ($this->isStale($auditRow)) {
            $this->abandonStaleJob($auditRow);

            return;
        }

        $userId = $auditRow->user_id;
        if ($userId === null || $userId === '') {
            Log::error('StreamQueryFromFastApi: audit row has no user_id — refusing to run unscoped', [
                'query_id' => $this->queryId,
            ]);
            $this->failBeforeStream('IDENTITY_UNRESOLVED', $auditRow);

            return;
        }

        // Audit 2026-06-27: carry workspace_id in the JWT so the FastAPI
        // lifecycle/RLS guard on the MAIN query path is actually enforced.
        // Derived from the project row.
        try {
            $workspaceId = $this->lookupWorkspaceId();
        } catch (\Throwable $e) {
            Log::error('StreamQueryFromFastApi: workspace lookup failed — refusing to run unscoped', [
                'project_id' => $this->projectId,
                'exception' => $e->getMessage(),
            ]);
            $this->failBeforeStream('WORKSPACE_LOOKUP_FAILED', $auditRow);

            return;
        }

        if ($workspaceId === null || $workspaceId === '') {
            Log::error('StreamQueryFromFastApi: project has no workspace — refusing to run unscoped', [
                'project_id' => $this->projectId,
            ]);
            $this->failBeforeStream('WORKSPACE_UNRESOLVED', $auditRow);

            return;
        }

        $jwt = app(FastApiJwtMinter::class)->mint(
            (string) $userId,
            $this->projectId,
            [], // roles — no role system yet (see B7 follow-up)
            $workspaceId,
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
            'trace_id' => $this->traceId(),
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
            // The chat thread this question belongs to, recorded as
            // answer_runs.session_id so a conversation's runs can be grouped
            // for replay. A UUID (QueryController drops anything else); older
            // FastAPI builds ignore the key.
            if ($this->conversationId !== null) {
                $payloadData['session_id'] = $this->conversationId;
            }
            $payload = json_encode($payloadData);

            $context = stream_context_create([
                'http' => [
                    'method' => 'POST',
                    'header' => array_values(array_filter([
                        'Content-Type: application/json',
                        'Accept: text/event-stream',
                        'Authorization: Bearer '.$jwt,
                        // Still REQUIRED by FastAPI: POST /internal/queries
                        // declares verify_service_key (Header(...), no
                        // default) as a router dependency alongside the JWT
                        // (app/services/auth.py, routers/queries.py). Do not
                        // drop it until that dependency is removed there.
                        'X-Service-Key: '.$serviceKey,
                        // W3C trace context. Without this FastAPI's
                        // StructuredAccessLogMiddleware mints an unrelated
                        // trace id and there is no join key between the two
                        // services' logs — you are left correlating Log
                        // Analytics by timestamp across a queue hop.
                        // Omitted entirely when the dispatcher had no trace
                        // context (console dispatch, replayed job): an empty
                        // header would fail the middleware's v00 validation
                        // and be replaced by a minted one anyway.
                        $this->traceparent !== null ? 'traceparent: '.$this->traceparent : null,
                        // The query id doubles as the request id, so a
                        // support ticket quoting one finds both sides.
                        'X-Request-ID: '.$this->queryId,
                    ])),
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
                'trace_id' => $this->traceId(),
            ]);

            if ($statusCode < 200 || $statusCode >= 300) {
                $body = (string) stream_get_contents($stream);
                fclose($stream);
                // The body stays server-side (log + encrypted audit row).
                // It can name internal hosts and carry framework error text,
                // none of which belongs in the browser (CHAT-20 / LAR-19).
                Log::warning('StreamQueryFromFastApi: FastAPI returned non-2xx', [
                    'query_id' => $this->queryId,
                    'status' => $statusCode,
                    'body' => substr($body, 0, 480),
                ]);
                // 503 is FastAPI's fail-closed answer when it could not
                // check the project's lifecycle (project_lifecycle_check_
                // unavailable, with Retry-After): transient and
                // retryable, and "the answer service returned an error"
                // says nothing about what to do.
                // 402/403 with a lifecycle detail is FastAPI refusing a
                // hibernated, archived or past-due project before it streams
                // (middleware/project_lifecycle.py). "Returned an error,
                // please try again" invited a Retry that fails the same way
                // every time; say what the state is instead.
                $lifecycle = in_array($statusCode, [402, 403], true)
                    ? (self::PROJECT_LIFECYCLE_REFUSALS[$this->errorDetail($body)] ?? null)
                    : null;
                if ($statusCode === 503) {
                    $this->broadcastError(
                        'The project could not be checked right now. Please try again in a few seconds.',
                        'SERVICE_UNAVAILABLE',
                    );
                } elseif ($lifecycle !== null) {
                    $this->broadcastError($lifecycle['message'], $lifecycle['code']);
                } else {
                    $this->broadcastError(
                        "The answer service returned an error (HTTP {$statusCode}). Please try again.",
                        $statusCode,
                    );
                }
                // Fall through to the shared audit finalisation instead of
                // returning — an early return left every non-2xx run's
                // audit row indistinguishable from "reserved but never
                // dispatched", corrupting latency/error analytics.
                $this->failedPayload = [
                    'event' => 'failed',
                    'query_id' => $this->queryId,
                    'code' => $lifecycle['code'] ?? (string) $statusCode,
                    'error' => 'FastAPI returned HTTP '.$statusCode.': '.substr($body, 0, 480),
                ];
            }

            if ($this->failedPayload === null) {
                // Read the SSE stream line by line. fgets() blocks waiting for data.
                $eventType = null;
                $dataBuffer = '';
                $eventCount = 0;

                $cancelled = false;
                // 0.0 so the first frame checks at once; then at most 1/s.
                $lastCancelCheck = 0.0;

                while (! feof($stream)) {
                    $line = fgets($stream);
                    if ($line === false) {
                        break;
                    }

                    // CHAT-18 — Stop in the browser used to only leave the
                    // channel; this job and the FastAPI run carried on for
                    // up to 180 s, holding one of the llm slots and billing
                    // the LLM. Checked at most once a second, between
                    // frames; closing the socket cancels the FastAPI run
                    // through its client-disconnect path.
                    if (microtime(true) - $lastCancelCheck >= 1.0) {
                        $lastCancelCheck = microtime(true);
                        if ($this->cancellationRequested()) {
                            $cancelled = true;
                            break;
                        }
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

                if (! $cancelled && $dataBuffer !== '') {
                    $this->dispatchSseEvent($eventType ?? 'delta', $dataBuffer);
                    $eventCount++;
                }

                fclose($stream);

                if ($cancelled && $this->completedPayload === null && $this->failedPayload === null) {
                    Log::info('StreamQueryFromFastApi: cancelled by the user', [
                        'query_id' => $this->queryId,
                        'events_dispatched' => $eventCount,
                    ]);
                    $this->failedPayload = [
                        'event' => 'failed',
                        'query_id' => $this->queryId,
                        'code' => 'CANCELLED',
                        'error' => 'Stopped by the user.',
                    ];
                    $this->dispatchSseEvent('failed', (string) json_encode($this->failedPayload));
                }

                Log::info('StreamQueryFromFastApi: stream complete', [
                    'query_id' => $this->queryId,
                    'events_dispatched' => $eventCount,
                ]);

                // A stream that ended without EITHER terminal payload was
                // truncated (FastAPI restart, revision swap, socket drop
                // mid-stream). Treating it as success left the audit row
                // half-written and — worse — broadcast nothing terminal, so
                // the browser hung until its watchdog. Synthesise a failed
                // terminal so both the client and the audit row learn the
                // truth.
                if ($this->completedPayload === null && $this->failedPayload === null) {
                    Log::warning('StreamQueryFromFastApi: stream truncated — no terminal event received', [
                        'query_id' => $this->queryId,
                        'events_dispatched' => $eventCount,
                    ]);
                    $this->failedPayload = [
                        'event' => 'failed',
                        'query_id' => $this->queryId,
                        'code' => 'STREAM_TRUNCATED',
                        'error' => 'The answer stream ended unexpectedly — please try again.',
                    ];
                    $this->dispatchSseEvent('failed', (string) json_encode($this->failedPayload));
                }
            }

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
                // Correct llm_model to the model that actually served.
                //
                // QueryController::store() stamps this column at RESERVATION
                // time from config — before a model has been chosen, let
                // alone called — so without this refresh it records a
                // configuration value, not a fact.
                //
                // The refresh used to read a "routing" SSE frame carrying
                // {tier, model, reason}. That frame never arrived: its
                // producer lived in the flat app/agent/orchestrator.py
                // module and was dropped when that module became a package,
                // leaving a consumer here and another in FastAPI's
                // queries.py with nothing upstream of either. The column
                // therefore held its dispatch-time default for every query
                // ever logged. GeoRAGResponse.llm_model replaces the whole
                // sentinel protocol: it rides the `completed` payload we
                // already read for text, citations and confidence.
                $servedBy = $this->completedPayload['llm_model'] ?? null;
                if (is_string($servedBy) && $servedBy !== '') {
                    $row->llm_model = $servedBy;
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
                $existing = is_array($row->metadata) ? $row->metadata : [];
                $guardCodes = $this->completedPayload['guard_error_codes'] ?? null;
                if (is_array($guardCodes) && $guardCodes !== []) {
                    $existing['guard_error_codes'] = array_values(array_filter(
                        $guardCodes,
                        fn ($c) => is_string($c) && $c !== '',
                    ));
                }
                // CHAT-8 — what GET /api/v1/queries/{id}/result needs to
                // re-render this answer in a tab that lost the `completed`
                // frame (reconnect, oversize, watchdog): the verdicts and
                // the run id, not just text + citations.
                foreach (['validation_state', 'answer_run_id', 'refusal_payload', 'degraded_sources'] as $key) {
                    if (array_key_exists($key, $this->completedPayload)) {
                        $existing[$key] = $this->completedPayload[$key];
                    }
                }
                $row->metadata = $existing;

                $row->save();
            } elseif ($row !== null && $this->failedPayload !== null) {
                // A failure has no `completed` payload and therefore no
                // served-by model. Leaving llm_model at its dispatch-time
                // value is the honest outcome here: it records what the
                // system intended to use, and the failure marker in
                // response_text says the attempt did not produce an answer.
                $row->response_text = $this->formatFailureMarker($this->failedPayload);
                $row->response_time_ms = $elapsed;
                $row->save();
            }
        } catch (\Throwable $e) {
            Log::error('StreamQueryFromFastApi failed', [
                'query_id' => $this->queryId,
                'exception' => $e->getMessage(),
            ]);

            // Broadcast FIRST — if the DB is the failing dependency, the
            // audit write below re-throws and the client would otherwise
            // never receive a terminal event.
            //
            // A generic message, not $e->getMessage() (CHAT-20 / LAR-19):
            // that text named the internal FastAPI URL ("Failed to open
            // stream to FastAPI: http://fastapi.<ns>:8000/...") or carried
            // SQL. The detail is in the log line above and the audit row.
            $this->broadcastError('The answer service could not be reached. Please try again.', 'INTERNAL');

            $elapsed = (int) ((microtime(true) - $this->startTime) * 1000);
            try {
                $row = QueryAuditLog::where('query_id', $this->queryId)->first();
                if ($row !== null) {
                    $row->response_text = '[error: '.substr($e->getMessage(), 0, 480).']';
                    $row->response_time_ms = $elapsed;
                    $row->save();
                }
            } catch (\Throwable $auditExc) {
                // A DB-caused failure must not mask the ORIGINAL exception
                // in the log/rethrow below.
                Log::warning('StreamQueryFromFastApi: failure-path audit write skipped', [
                    'query_id' => $this->queryId,
                    'exception' => $auditExc->getMessage(),
                ]);
            }

            throw $e;
        }
    }

    /**
     * Terminal-failure hook (C8).
     *
     * Invoked by Horizon when the job exhausts its attempts OR is killed at
     * the `$timeout` boundary mid-stream. In the timeout case the catch
     * block in handle() does NOT run (the worker dies inside fgets()), so
     * without this method the audit row and the frontend are both left in
     * limbo.
     *
     * Differences from the in-handle catch:
     *   1. Runs on a FRESH deserialised instance — the captured $startTime
     *      is 0. Elapsed time comes from the audit row's dispatched_at.
     *   2. Broadcasts `event: 'failed'` (not `'error'`), matching the
     *      FastAPI SSE vocabulary + the frontend's terminal handler.
     *
     * Order matters (CHAT-13): the terminal broadcast goes out BEFORE the
     * audit write, and the write is guarded. When the database is the
     * failing dependency (RDS stopped by the nightly sweep, connection
     * exhaustion) an unguarded write used to throw first, so no terminal
     * frame was sent and the browser waited for its watchdog.
     *
     * A row that handle() already finalised is left alone (CHAT-1 / CHAT-20):
     *   - a successful answer is never overwritten with a [FAILED marker —
     *     that is what a re-queued duplicate of a finished job used to do;
     *   - an `[error:` marker means handle() already broadcast its own
     *     terminal and recorded the cause, so neither is repeated.
     */
    public function failed(\Throwable $e): void
    {
        Log::error('StreamQueryFromFastApi: terminal failure', [
            'query_id' => $this->queryId,
            'exception' => get_class($e),
            'message' => $e->getMessage(),
        ]);

        $row = null;
        $rowReadable = true;
        try {
            $row = QueryAuditLog::where('query_id', $this->queryId)->first();
        } catch (\Throwable $lookupError) {
            $rowReadable = false;
            Log::warning('StreamQueryFromFastApi::failed: audit row lookup failed', [
                'query_id' => $this->queryId,
                'exception' => $lookupError->getMessage(),
            ]);
        }

        $existing = $row?->response_text;
        $finalisedByHandle = is_string($existing) && $existing !== ''
            && ! str_starts_with($existing, '[FAILED');

        if ($finalisedByHandle) {
            Log::info('StreamQueryFromFastApi::failed: row already finalised by handle(), not overwriting', [
                'query_id' => $this->queryId,
                'kind' => str_starts_with($existing, '[error:') ? 'failure' : 'answer',
            ]);

            return;
        }

        // Broadcast a terminal `failed` event so the client stops waiting.
        // Defensive: if the broadcast itself fails (Reverb down, network
        // blip) the handler must still reach the audit write. Swallow + log.
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

        if ($row === null) {
            if ($rowReadable) {
                Log::warning('StreamQueryFromFastApi::failed: audit row not found', [
                    'query_id' => $this->queryId,
                ]);
            }

            return;
        }

        if (is_string($existing) && str_starts_with($existing, '[FAILED')) {
            // Idempotent: a repeat invocation keeps the first marker.
            return;
        }

        try {
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
        } catch (\Throwable $auditError) {
            Log::warning('StreamQueryFromFastApi::failed: audit write skipped', [
                'query_id' => $this->queryId,
                'exception' => $auditError->getMessage(),
            ]);
        }
    }

    /**
     * The audit row for this query. A seam so tests can run the stream
     * plumbing without DB fixtures; throws when the DB is unavailable.
     */
    protected function lookupAuditRow(): ?QueryAuditLog
    {
        return QueryAuditLog::where('query_id', $this->queryId)->first();
    }

    /**
     * The workspace that owns this job's project, or null when the project
     * does not exist. Throws when the DB is unavailable.
     */
    protected function lookupWorkspaceId(): ?string
    {
        $workspaceId = DB::table('silver.projects')
            ->where('project_id', $this->projectId)
            ->value('workspace_id');

        return $workspaceId !== null ? (string) $workspaceId : null;
    }

    /**
     * User-facing code on the terminal frame for every pre-stream identity /
     * workspace failure. The specific reason (AUDIT_LOOKUP_FAILED, ...) is an
     * implementation detail: the UI renders the code as a title ("Reason:
     * Audit Lookup Failed"), so it stays in the log and the audit marker only.
     */
    private const ACCESS_CHECK_FAILED = 'ACCESS_CHECK_FAILED';

    /** Reasons where a lookup itself errored: nothing is known about access. */
    private const ACCESS_LOOKUP_ERROR_REASONS = ['AUDIT_LOOKUP_FAILED', 'WORKSPACE_LOOKUP_FAILED'];

    /**
     * End the job with a terminal `failed` frame BEFORE FastAPI is called,
     * because the caller's identity or workspace could not be established.
     *
     * Goes through dispatchSseEvent() so the terminal-delivery fallback
     * applies, then records the failure on the audit row when there is one.
     * The frame carries the single user-facing code ACCESS_CHECK_FAILED and
     * a message that separates "could not verify" (a lookup errored; retry)
     * from "no access" (the identity or workspace did not resolve). The
     * specific `$reason` goes to the log and the audit marker.
     */
    private function failBeforeStream(string $reason, ?QueryAuditLog $row): void
    {
        $lookupErrored = in_array($reason, self::ACCESS_LOOKUP_ERROR_REASONS, true);
        $payload = [
            'event' => 'failed',
            'query_id' => $this->queryId,
            'code' => self::ACCESS_CHECK_FAILED,
            'error' => $lookupErrored
                ? 'We could not verify your access to this project right now, so the query was not run. Please try again in a few seconds.'
                : 'You do not appear to have access to this project, so the query was not run. If you think this is a mistake, contact your administrator.',
        ];
        Log::warning('StreamQueryFromFastApi: pre-stream access check failed', [
            'query_id' => $this->queryId,
            'reason' => $reason,
        ]);
        $this->dispatchSseEvent('failed', (string) json_encode($payload));

        if ($row === null) {
            return;
        }

        try {
            // The audit marker keeps the specific reason; only the browser
            // sees the generic code.
            $row->response_text = $this->formatFailureMarker(['code' => $reason] + $payload);
            $row->response_time_ms = (int) ((microtime(true) - $this->startTime) * 1000);
            $row->save();
        } catch (\Throwable $e) {
            Log::warning('StreamQueryFromFastApi: pre-stream failure audit write skipped', [
                'query_id' => $this->queryId,
                'exception' => $e->getMessage(),
            ]);
        }
    }

    /**
     * Cache key the cancel endpoint sets and handle() polls (CHAT-18).
     *
     * A pure function of the id — no state is held on the class.
     */
    public static function cancelCacheKey(string $queryId): string
    {
        return 'georag:query-cancel:'.$queryId;
    }

    /**
     * Has the user pressed Stop? A cache failure reads as "no" — the
     * stream carries on, which is the pre-cancel behaviour.
     */
    private function cancellationRequested(): bool
    {
        try {
            return Cache::has(self::cancelCacheKey($this->queryId));
        } catch (\Throwable $e) {
            Log::debug('StreamQueryFromFastApi: cancel flag lookup failed', [
                'query_id' => $this->queryId,
                'exception' => $e->getMessage(),
            ]);

            return false;
        }
    }

    /**
     * Was this job picked up too long after /start to be worth running?
     *
     * Only a row whose dispatched_at is known can be stale; a missing row
     * (unit tests, DB blip) runs as before.
     */
    private function isStale(?QueryAuditLog $row): bool
    {
        $threshold = (int) config('services.fastapi.queue_stale_after', 110);
        if ($threshold <= 0 || $row === null || $row->dispatched_at === null) {
            return false;
        }

        return $row->dispatched_at->diffInSeconds(now(), true) > $threshold;
    }

    /**
     * Terminal for a stale job: tell the browser (if it is still there) and
     * mark the audit row, without calling FastAPI.
     */
    private function abandonStaleJob(QueryAuditLog $row): void
    {
        $waited = $row->dispatched_at?->diffInSeconds(now(), true);

        Log::warning('StreamQueryFromFastApi: abandoning stale job (queued too long)', [
            'query_id' => $this->queryId,
            'queued_seconds' => $waited,
            'threshold_seconds' => (int) config('services.fastapi.queue_stale_after', 110),
        ]);

        $payload = [
            'event' => 'failed',
            'query_id' => $this->queryId,
            'code' => 'QUEUE_STALE',
            'error' => 'The query waited too long for a free worker and was not run. Please try again.',
        ];
        $this->dispatchSseEvent('failed', (string) json_encode($payload));

        try {
            $row->response_text = $this->formatFailureMarker($payload);
            $row->response_time_ms = $waited !== null ? (int) ($waited * 1000) : null;
            $row->save();
        } catch (\Throwable $e) {
            Log::warning('StreamQueryFromFastApi: stale-job audit write skipped', [
                'query_id' => $this->queryId,
                'exception' => $e->getMessage(),
            ]);
        }
    }

    /**
     * The `completed` frame as it can actually be delivered (CHAT-5).
     *
     * The full GeoRAGResponse can carry a 3D drill-trace card of up to 200
     * collars x 50 points plus 1000 intervals — about a megabyte of JSON.
     * Reverb rejects a request over REVERB_MAX_REQUEST_SIZE, the broadcast
     * throws, and the chat never sees a terminal frame. Over the budget,
     * this keeps everything the answer's integrity depends on (text,
     * citations, the verdicts, the run id) and drops the bulky
     * visualisation fields, saying which ones went.
     *
     * @param array<string, mixed> $payload
     *
     * @return array<string, mixed>
     */
    private function fitCompletedFrame(array $payload): array
    {
        $budget = (int) config('services.fastapi.completed_frame_budget_bytes', 700_000);
        $encoded = json_encode($payload);
        if ($budget <= 0 || ($encoded !== false && strlen($encoded) <= $budget)) {
            return $payload;
        }

        $slim = array_intersect_key($payload, array_flip(self::COMPLETED_FRAME_ESSENTIAL_KEYS));
        $slim['payload_truncated'] = true;
        $slim['truncated_fields'] = array_values(array_filter(
            array_keys(array_diff_key($payload, $slim)),
            fn (string $key): bool => $payload[$key] !== null && $payload[$key] !== [],
        ));

        // Still too big means the citations themselves are the bulk. Keep
        // them all if at all possible — they are the answer's evidence —
        // but a frame that cannot be delivered helps nobody.
        $encodedSlim = json_encode($slim);
        if ($encodedSlim !== false && strlen($encodedSlim) > $budget && is_array($slim['citations'] ?? null)) {
            $slim['citations_total'] = count($slim['citations']);
            $slim['citations'] = array_slice($slim['citations'], 0, self::COMPLETED_FRAME_MAX_CITATIONS);
        }

        Log::warning('StreamQueryFromFastApi: completed frame over Reverb budget, broadcasting slim frame', [
            'query_id' => $this->queryId,
            'bytes' => $encoded !== false ? strlen($encoded) : null,
            'budget' => $budget,
            'dropped' => $slim['truncated_fields'],
        ]);

        return $slim;
    }

    /**
     * Keys a slim `completed` frame keeps — what Chat.tsx needs to render a
     * verified, cited answer and its verdict (stamping fields included).
     *
     * @var list<string>
     */
    private const COMPLETED_FRAME_ESSENTIAL_KEYS = [
        'event', 'query_id', 'event_seq', 'event_id', 'event_name', 'trace_id',
        'text', 'citations', 'confidence', 'validation_state', 'answer_run_id',
        'refusal_payload', 'guard_error_codes', 'llm_model', 'sources_used',
        'multi_turn_resolution', 'validation_warnings',
        // Sources that failed for this answer ("Document ranking
        // (temporarily unavailable)"). Without it a partial answer renders
        // exactly like a complete one.
        'degraded_sources',
    ];

    /**
     * FastAPI's project-lifecycle refusals (`detail` of its 402/403), as the
     * code and message the chat shows. Codes match RefusalPanel's headings.
     *
     * @var array<string, array{code: string, message: string}>
     */
    private const PROJECT_LIFECYCLE_REFUSALS = [
        'project_hibernated' => [
            'code' => 'PROJECT_HIBERNATED',
            'message' => 'This project is hibernated, so it cannot answer questions until it is reactivated.',
        ],
        'project_archived' => [
            'code' => 'PROJECT_ARCHIVED',
            'message' => 'This project is archived and no longer answers questions.',
        ],
        'project_past_due' => [
            'code' => 'PROJECT_PAST_DUE',
            'message' => 'Questions are paused for this project until its account is brought up to date.',
        ],
    ];

    /**
     * The `detail` string of a FastAPI error body, or '' when there is none.
     */
    private function errorDetail(string $body): string
    {
        $decoded = json_decode($body, true);

        return is_array($decoded) && is_string($decoded['detail'] ?? null) ? $decoded['detail'] : '';
    }

    /** Citation cap for a slim frame that is still over budget. */
    private const COMPLETED_FRAME_MAX_CITATIONS = 100;

    /** Completed event payload — captured for audit log update. */
    private ?array $completedPayload = null;

    /** Failed event payload — captured for audit log failure update. */
    private ?array $failedPayload = null;

    private function formatFailureMarker(array $payload): string
    {
        $code = $payload['code'] ?? 'UNKNOWN';
        $error = $payload['error'] ?? $payload['message'] ?? 'unspecified';

        return '[error: '.$code.' — '.substr((string) $error, 0, 480).']';
    }

    private float $startTime = 0;

    /**
     * Consecutive failed frame broadcasts in this run, after which per-token
     * deltas are no longer sent. Instance state on a queue job (one instance
     * per attempt), not static — nothing outlives the job.
     */
    private const BROADCAST_FAILURE_CUTOFF = 5;

    private int $consecutiveBroadcastFailures = 0;

    private bool $deltaCutoffLogged = false;

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
        }
        // A `routing` branch used to live here, capturing {tier, model,
        // reason} for the audit row. It has been removed along with its
        // counterpart in FastAPI's queries.py: no code emitted that frame
        // — the producer was lost in the orchestrator package refactor —
        // and nothing read the `__routing__:` marker it wrote into
        // sources_used, in PHP or in React. Both halves of a protocol with
        // neither producer nor consumer read as working wiring, which is
        // how llm_model went unnoticed. The model now rides the `completed`
        // payload as GeoRAGResponse.llm_model.

        // Defensive: a single SSE frame failing to broadcast (size limit,
        // transient Reverb hiccup) MUST NOT abort the stream — we still
        // want to capture the terminal completed/failed payload for the
        // audit row, and subsequent frames may broadcast fine. Swallow
        // here and let handle() complete normally. The frontend's
        // timeout watchdog catches the rare case where every frame fails.
        //
        // Except for the terminal frame (CHAT-5): swallowing THAT one left
        // the browser with every delta and no end, and the watchdog then
        // blamed the realtime channel two minutes later. A terminal whose
        // broadcast fails is followed by a small `failed` that says the
        // answer exists (recoverable=true), so Chat.tsx fetches it from
        // GET /api/v1/queries/{id}/result instead of waiting.
        $wirePayload = $eventType === 'completed' ? $this->fitCompletedFrame($payload) : $payload;

        // Circuit breaker for per-token deltas. Every broadcast is inline
        // (ShouldBroadcastNow) and a dead Reverb costs up to the client
        // timeout per frame, so a few hundred deltas against a broken
        // channel would hold this llm slot for minutes and still deliver
        // nothing. After BROADCAST_FAILURE_CUTOFF consecutive failures the
        // deltas stop; non-delta frames (status, bind, citation, and above
        // all the terminal) are still attempted, a success resets the run,
        // and the terminal `completed` carries the full text anyway.
        if ($eventType === 'delta' && $this->consecutiveBroadcastFailures >= self::BROADCAST_FAILURE_CUTOFF) {
            if (! $this->deltaCutoffLogged) {
                $this->deltaCutoffLogged = true;
                Log::warning('StreamQueryFromFastApi: delta broadcasting suspended after consecutive failures', [
                    'query_id' => $this->queryId,
                    'consecutive_failures' => $this->consecutiveBroadcastFailures,
                ]);
            }

            return;
        }

        try {
            broadcast(new QueryStreamEvent($this->channel, $eventType, $wirePayload));
            $this->consecutiveBroadcastFailures = 0;
            $this->deltaCutoffLogged = false;
        } catch (\Throwable $broadcastError) {
            $this->consecutiveBroadcastFailures++;
            Log::warning('StreamQueryFromFastApi: SSE frame broadcast failed', [
                'query_id' => $this->queryId,
                'event_type' => $eventType,
                'consecutive_failures' => $this->consecutiveBroadcastFailures,
                'broadcast_exception' => $broadcastError->getMessage(),
            ]);

            if ($eventType === 'completed' || $eventType === 'failed') {
                $this->broadcastTerminalFallback($eventType);
            }
        }
    }

    /**
     * Last-resort terminal after a terminal frame failed to broadcast.
     *
     * Deliberately tiny so it fits any Reverb limit. For a lost `completed`
     * it marks the answer recoverable: the job finalises the audit row right
     * after the stream drains, and the result endpoint serves it from there.
     */
    private function broadcastTerminalFallback(string $lostEventType): void
    {
        $answerExists = $lostEventType === 'completed';

        try {
            broadcast(new QueryStreamEvent($this->channel, 'failed', [
                'event' => 'failed',
                'query_id' => $this->queryId,
                'code' => $answerExists ? 'DELIVERY_FAILED' : 'JOB_FAILED',
                'error' => $answerExists
                    ? 'The answer was produced but could not be delivered over the realtime channel. Loading it now…'
                    : 'Your query could not be completed. Please try again.',
                'recoverable' => $answerExists,
            ]));
        } catch (\Throwable $fallbackError) {
            Log::error('StreamQueryFromFastApi: terminal fallback broadcast failed', [
                'query_id' => $this->queryId,
                'lost_event_type' => $lostEventType,
                'broadcast_exception' => $fallbackError->getMessage(),
            ]);
        }
    }

    /**
     * Broadcast an error event so the frontend can surface a meaningful message
     * rather than silently timing out.
     */
    private function broadcastError(string $message, int|string $code): void
    {
        // Defensive: a broken broadcast layer must not propagate back into
        // the caller's catch block — handle() relies on broadcastError()
        // running to completion so it can return cleanly. If Reverb is
        // unreachable, the frontend's timeout watchdog (P0.2) will fire
        // its own terminal error UI.

        // `failed`, not `error`. The SSE vocabulary this job relays is
        // status / bind / delta / citation / completed / failed -- FastAPI
        // never emits `error`, this class's own docblock does not list it,
        // and failed()'s docblock says in so many words "Broadcasts
        // event: 'failed' (not 'error')". This method was the one place
        // producing a frame name outside the contract.
        //
        // It worked only because Chat.tsx happens to accept
        // `eventType === 'failed' || eventType === 'error'`. That tolerance
        // was the single thing standing between this path and a permanent
        // spinner: tighten the frontend to the documented vocabulary -- a
        // reasonable tidy-up for anyone reading the three docblocks -- and
        // every FastAPI 4xx/5xx becomes a chat that never terminates.
        //
        // Safe to change now rather than later precisely because the
        // frontend accepts both, and reads `event.error ?? event.message`,
        // so this is behaviourally identical today and correct afterwards.
        try {
            broadcast(new QueryStreamEvent(
                $this->channel,
                'failed',
                [
                    'event' => 'failed',
                    'query_id' => $this->queryId,
                    'code' => $code,
                    // `error` is the key FastAPI's `failed` frame and
                    // failed() use; `message` kept for older clients.
                    'error' => $message,
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
            // Newest N turns, then restored to chronological order — the
            // ascending+limit form silently returned the OLDEST 20 turns,
            // so long threads resolved pronouns against ancient context
            // and never the immediately preceding turn.
            //
            // Ordered by `position` (CHAT-2 / LAR-5). This used to order
            // and select by `id`, a column chat_messages does not have (its
            // key is message_id): Postgres raised, the catch below returned
            // [], and no chat turn ever reached FastAPI with its history.
            // created_at cannot stand in — a sync re-inserts the whole
            // thread within one second — so position is the order, and
            // message_id only makes ties deterministic.
            $messages = ChatMessage::query()
                ->where('conversation_id', $this->conversationId)
                ->orderByDesc('position')
                ->orderByDesc('created_at')
                ->orderByDesc('message_id')
                ->limit(self::HISTORY_MAX_TURNS)
                ->get(['message_id', 'conversation_id', 'role', 'content', 'metadata', 'position'])
                ->reverse()
                ->values();
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
