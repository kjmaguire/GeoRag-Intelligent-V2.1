"""RAG query endpoint — the primary Laravel<->FastAPI contract surface.

POST /internal/queries
---------------------
Receives a natural-language geological query from Laravel, runs it through the
Pydantic AI geo_agent, and streams the response back as Server-Sent Events
(SSE). Laravel's GeoRagService consumes the stream, forwards delta tokens to
the React frontend via Reverb, and waits for the 'completed' event to persist
the final GeoRAGResponse.

SSE event types
---------------
  delta      — incremental token chunk from the LLM
               data: {"token": "<token text>"}

  citation   — inline citation emitted as it is resolved
               data: {"citation_id": "[DATA-1]", "citation_type": "DATA",
                       "source_chunk_id": "...", "document_title": "...",
                       "relevance_score": 0.92}

  completed  — full GeoRAGResponse JSON; signals the stream is done
               data: <GeoRAGResponse serialised as JSON>

  failed     — error payload; no 'completed' event follows
               data: {"error": "<message>", "code": "<error_code>"}

Architecture references
-----------------------
  Section 07c Option A — SSE streaming contract
  Section 07d          — request / response shapes
  Section 04i          — 6-layer hallucination prevention (agent plug-in point)
  Section 05           — deterministic query flow

Streaming strategy
------------------
Pydantic AI's run_stream() returns a StreamedRunResult whose stream_text()
async generator yields delta tokens as the LLM produces them.  Because the
agent has a structured output type (GeoRAGResponse) the text stream ends when
the model closes the JSON block; stream_text(delta=True) surfaces the raw
token fragments.  After the text stream closes, get_output() returns the
validated GeoRAGResponse which we use to emit citation and completed events.

If stream_text produces no tokens (e.g. the model returned a pure JSON blob
without intermediate text), the response text is split on word boundaries and
emitted as synthetic delta events so the frontend always sees a progressive
stream rather than a sudden completed event.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.agent.deps import AgentDeps
from app.agent.event_stamper import EventStamper
from app.config import settings
from app.models.rag import GeoRAGResponse
from app.services.auth import UserContext, extract_user_context, verify_service_key
from app.services.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["queries"])


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    """Payload sent by Laravel's GeoRagService to POST /internal/queries."""

    query: str = Field(..., min_length=1, max_length=4096, description="Natural-language geological query")
    project_id: str = Field(..., min_length=1, description="UUID of the active project scope")
    # Phase 3 / Step 3.2 — optional 12-field context envelope + Field/Office
    # mode. Forwarded by the Laravel bridge job; when the agentic-retrieval
    # flag is on, this drives intent-routing overrides + retrieval filters +
    # prompt suffixes. Shape: ``ContextEnvelope.model_dump()`` from
    # ``app.agent.agentic_retrieval.context_envelope``. Backward compatible:
    # legacy callers that don't send this field get the default empty envelope.
    context_envelope: dict | None = Field(
        default=None,
        description=(
            "Optional 12-field context envelope + mode (field/office). When "
            "omitted the envelope is treated as fully unspecified — every "
            "missing field surfaces in OIUR uncertainty per Phase 2 Step 2.4."
        ),
    )
    # Plan §3e — multi-turn resolution. Optional conversation history
    # the Laravel bridge forwards on every query in a thread. Each entry
    # is a turn dict; the FastAPI agentic graph hands the list to the
    # resolve_node, which expands pronouns / demonstratives / comparatives
    # against the history before classification. When
    # MULTI_TURN_RESOLUTION_ENABLED is False (default), resolve_node
    # is a no-op and history is ignored.
    #
    # Shape per entry:
    #   {
    #     "turn_index": int,      # 0 = oldest, N = most recent
    #     "role": "user" | "assistant",
    #     "text": str,
    #     "entity_mentions": [    # optional; resolve_node falls back to
    #         {                   # heuristic extraction if empty
    #             "surface_form": "PLS-22-08",
    #             "entity_type": "hole" | "property" | ... ,
    #             "turn_index": int,
    #             "normalised_id": str | None,
    #         },
    #         ...
    #     ]
    #   }
    history: list[dict] | None = Field(
        default=None,
        max_length=50,
        description=(
            "Optional list of prior conversation turns for §3e multi-turn "
            "resolution. Bounded at 50 turns (the bridge should pass the "
            "most recent N; older context is dropped). When omitted or "
            "MULTI_TURN_RESOLUTION_ENABLED=False, the agentic graph runs "
            "single-turn."
        ),
    )


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_event(event: str, data: Any) -> str:
    """Serialise a single SSE frame.

    Format per the HTML Living Standard:
        event: <event-name>\\n
        data: <JSON string>\\n
        \\n
    The trailing blank line terminates the event.
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _sse_keepalive() -> str:
    """SSE comment frame to prevent proxy/load-balancer connection closure."""
    return ": keepalive\n\n"


def _extract_tool_results(messages: list[Any]) -> list[tuple[str, Any]]:
    """Walk the Pydantic AI message history and extract (tool_name, result) tuples.

    Tool calls appear in ModelRequest messages as ToolReturnPart items.
    Each part has .tool_name and .content (the tool's return value).
    """
    results: list[tuple[str, Any]] = []
    for msg in messages:
        parts = getattr(msg, "parts", None)
        if not parts:
            continue
        for part in parts:
            # ToolReturnPart has tool_name and content fields
            if hasattr(part, "tool_name") and hasattr(part, "content"):
                tool_name = getattr(part, "tool_name", None)
                content = getattr(part, "content", None)
                if tool_name and content is not None:
                    results.append((tool_name, content))
    return results


# ---------------------------------------------------------------------------
# Agent stream
# ---------------------------------------------------------------------------


async def _agent_rag_stream(
    body: QueryRequest,
    app_state: Any,
    user: UserContext | None = None,
    stamper: EventStamper | None = None,
) -> AsyncIterator[str]:
    """Run the full geo_agent RAG pipeline and yield SSE events.

    Flow:
      1. Build AgentDeps from app.state pools (assembled once per request).
      2. Open run_stream context — Pydantic AI sends the prompt + system
         prompt to Ollama, which starts the token stream.
      3. Yield delta events for each token fragment from stream_text().
      4. After stream_text() exhausts, call get_output() to get the
         validated GeoRAGResponse.
      5. If the text stream produced no tokens (model returned a pure JSON
         blob), synthesise delta events by splitting response.text on spaces.
      6. Emit citation events for each citation in order of appearance.
      7. Emit the completed event with the full serialised GeoRAGResponse.

    The overall deadline is settings.TIMEOUT_GATHER_S.  If the agent run
    exceeds this deadline we yield a failed event.  Per-tool timeouts are
    enforced inside each tool function.

    Module 7 Phase B Chunk 1 — stamper param:
      Every SSE frame is enriched with event_seq + event_id + answer_run_id
      (+ trace_id when OTel is active) and written to the Redis ring buffer for
      idempotent replay.  The stamper is pre-constructed by post_query() before
      the StreamingResponse is opened, using a request-scoped UUID.
    """
    from app.config import settings

    # Module 7 Phase B Chunk 1 — per-request Redis client + stamper helper.
    # stamper may be None in unit-test paths; guard defensively throughout.
    _redis_client = getattr(app_state, "redis_client", None)

    async def _stamped_event(event_name: str, data: dict[str, Any]) -> str:
        """Stamp data with event_seq/event_id, persist to Redis, return SSE frame.

        For ``delta`` events the caller renames the existing ``seq`` field to
        ``token_seq`` before passing data here so the two counters are distinct:
          - token_seq: per-token counter (unchanged from Module 5)
          - event_seq: per-frame counter across ALL event types (new M7B1)

        trace_id is None until Module 10 wires OTel; field is always present
        so the frontend can parse it without defensive checks.
        """
        if stamper is not None:
            seq, eid = stamper.next()
            # Prefer a persisted answer_run_id supplied by the payload
            # (GeoRAGResponse.answer_run_id, stamped by the orchestrator
            # after the silver.answer_runs INSERT) over the streaming-
            # session UUID owned by the stamper. The stamper UUID is only
            # meaningful for the Redis ring-buffer key during this request;
            # it is never persisted to PG, so emitting it on the wire
            # breaks the Retrieval Inspector deep link
            # (/retrieval/{answer_run_id}).
            _payload_run_id = data.get("answer_run_id")
            _effective_run_id = (
                str(_payload_run_id)
                if _payload_run_id is not None
                else str(stamper.answer_run_id)
            )
            enriched: dict[str, Any] = {
                **data,
                "event_seq": seq,
                "event_id": eid,
                "answer_run_id": _effective_run_id,
                "trace_id": stamper.trace_id,  # None until Module 10
                "event_name": event_name,
            }
            await stamper.push_to_redis(_redis_client, event_name, enriched)
        else:
            enriched = data
        return _sse_event(event_name, enriched)

    deps = AgentDeps(
        pg_pool=app_state.pg_pool,
        qdrant_client=app_state.qdrant_client,
        neo4j_driver=app_state.neo4j_driver,
        project_id=body.project_id,
        embedding_model=app_state.embedding_model,
        reranker=getattr(app_state, "reranker", None),
        # B2 — pass the pooled clients so the orchestrator doesn't reopen
        # TLS + pools per request. getattr() guards older startup paths in
        # which the pool wasn't attached (e.g., non-anthropic backends).
        redis_client=getattr(app_state, "redis_client", None),
        anthropic_client=getattr(app_state, "anthropic_client", None),
        # P1 #13 — pooled httpx client for the OpenAI-compatible backend.
        # When None the orchestrator falls back to ad-hoc construction;
        # this preserves the test path that doesn't go through lifespan.
        openai_http_client=getattr(app_state, "openai_http_client", None),
        # B7 — user scope from the JWT (None when the request used only
        # X-Service-Key auth during the graceful rollout). Tools that want
        # user-level RBAC check deps.user_id and degrade gracefully when None.
        user_id=(user.user_id if user else None),
        user_roles=(user.roles if user else ()),
    )

    # B7 — defensive check that JWT project_id matches request body.
    # Mismatched project between signed claim and body indicates either a
    # stale token or a cross-project request-forgery attempt. We log but
    # don't hard-fail during graceful rollout so legacy clients that don't
    # yet mint a project-scoped JWT continue to work.
    if user and user.project_id and user.project_id != body.project_id:
        logger.warning(
            "agent_rag_stream: JWT project_id=%s does not match body project_id=%s; "
            "using body project for retrieval scope",
            user.project_id,
            body.project_id,
        )

    from app.agent.log_safe import query_hash as _query_hash  # noqa: PLC0415
    logger.info(
        "agent_rag_stream: starting run project=%s query_hash=%s",
        body.project_id,
        _query_hash(body.query),
    )

    token_count = 0
    # P1 #16 — first-token latency. Captured at SSE-stream open and
    # observed on the histogram the first time we yield a `delta` event.
    import time as _time_mod  # noqa: PLC0415
    stream_started = _time_mod.monotonic()
    first_token_observed = False

    # SSE keepalive to prevent proxy timeout during orchestration.
    yield _sse_keepalive()

    # Phase 1: emit the initial status so the frontend has something to
    # render while the classifier + cache lookup spin up. The orchestrator
    # will push more granular status strings via the callback below.
    yield await _stamped_event("status", {"message": "Analyzing query…"})

    # Bridge between the orchestrator (which runs as a single awaited
    # coroutine) and this SSE generator (which yields frames). The
    # orchestrator pushes phase strings onto the queue; this loop drains
    # them into status events while the run executes in a background task.
    status_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def _push_status(message: str) -> None:
        # B1 — routing events are emitted through the same callback with
        # a sentinel prefix so the orchestrator stays agnostic about the
        # transport. Unwrap here into a structured "routing" frame.
        if isinstance(message, str) and message.startswith("__routing__:"):
            parts = message.split(":", 3)
            # Format: __routing__:<tier>:<model>[:<reason>]
            tier = parts[1] if len(parts) > 1 else "unknown"
            model = parts[2] if len(parts) > 2 else "unknown"
            reason = parts[3] if len(parts) > 3 else "classifier"
            await status_queue.put(("routing", {"tier": tier, "model": model, "reason": reason}))
            return
        await status_queue.put(("status", message))

    # P0 #5 — sequence counter + token pump. The Anthropic streaming path
    # calls this once per `text_delta` chunk; we re-emit each chunk as an
    # SSE `delta` event so the frontend can render progressively. On the
    # OpenAI-compatible backend this callback is never invoked and the
    # Phase-3 fallback below still synthesises deltas from the final text.
    token_seq = 0

    async def _push_token(chunk: str) -> None:
        nonlocal token_seq
        await status_queue.put(("delta", {"token": chunk, "seq": token_seq}))
        token_seq += 1

    # Eval 02 follow-up (2026-05-20) — citations-bound-pre-tokens.
    # The orchestrator invokes this callback after evidence is bound
    # (Stage 1) and BEFORE the LLM call streams the first token. The
    # SSE consumer renders the citation chips immediately so the
    # geologist never sees an unanchored answer.
    async def _push_bind(payload: dict) -> None:
        await status_queue.put(("bind", payload))

    async def _run_and_finalise() -> None:
        import time as _time
        started = _time.monotonic()
        outcome = "completed"
        try:
            from app.agent.orchestrator import (
                run_deterministic_rag,
                set_active_context_envelope,
                set_active_history,
            )
            # Phase 3 / Step 3.2 — parse the optional context envelope from
            # the request and stash it on a contextvar so the orchestrator's
            # agentic-retrieval dispatch can forward it without changing
            # run_deterministic_rag's signature (which would ripple to every
            # existing test caller).
            parsed_envelope = None
            if body.context_envelope is not None:
                try:
                    from app.agent.agentic_retrieval import ContextEnvelope
                    parsed_envelope = ContextEnvelope.model_validate(body.context_envelope)
                except Exception:
                    logger.exception(
                        "queries: failed to parse context_envelope — proceeding with None"
                    )
                    parsed_envelope = None
            set_active_context_envelope(parsed_envelope)
            # Plan §3e — stash conversation history (when supplied by
            # the Laravel bridge) for the orchestrator's agentic-retrieval
            # dispatch to thread into run_agentic_retrieval(history=...).
            set_active_history(body.history)
            async with asyncio.timeout(settings.TIMEOUT_GATHER_S):
                result = await run_deterministic_rag(
                    query=body.query,
                    deps=deps,
                    status_callback=_push_status,
                    token_callback=_push_token,
                    bind_callback=_push_bind,
                )
            await status_queue.put(("done", result))
        except asyncio.TimeoutError:
            outcome = "timeout"
            logger.error(
                "agent_rag_stream: overall deadline exceeded (%.1fs) project=%s",
                settings.TIMEOUT_GATHER_S,
                body.project_id,
            )
            await status_queue.put(("timeout", None))
        except Exception as exc:
            # §35.1 — workspace cost ceiling: surface as a structured
            # quota-exceeded event so the SSE stream can translate it
            # into HTTP 429 + Retry-After. Everything else is a generic
            # error.
            try:
                from app.agent.llm_calls import WorkspaceQuotaExceeded  # noqa: PLC0415
                if isinstance(exc, WorkspaceQuotaExceeded):
                    outcome = "quota_exceeded"
                    await status_queue.put(("quota_exceeded", exc))
                    return
            except ImportError:
                pass
            outcome = "error"
            await status_queue.put(("error", exc))
        finally:
            # #1 signal-harvesting — the single call to QUERY_DURATION is
            # per orchestrator run (not per status frame), so it matches
            # the Grafana dashboard panel's expected cardinality.
            try:
                from app.metrics import (  # noqa: PLC0415
                    PG_POOL_SATURATION,
                    PG_POOL_SIZE,
                    QUERIES_TOTAL,
                    QUERY_DURATION,
                )
                from app.config import settings as _s  # noqa: PLC0415
                QUERY_DURATION.labels(outcome=outcome).observe(
                    _time.monotonic() - started
                )
                # `tier` and `backend` pulled from settings rather than
                # captured from the per-run routing — when the orchestrator
                # downshifts mid-run we still want the ROUTING_DECISIONS
                # label to reflect that; QUERIES_TOTAL is the coarse
                # "finished at all" counter.
                QUERIES_TOTAL.labels(
                    tier="unknown",  # routing counter carries tier detail
                    outcome=outcome,
                    backend=_s.LLM_BACKEND,
                ).inc()

                # P1 #36b — sample pool saturation. asyncpg's get_size()
                # returns the count of connections currently HELD by the
                # pool (idle + in-use); get_max_size() is the configured
                # ceiling. Ratio near 1.0 means we're queueing on acquire
                # and need to bump max_size (or PgBouncer default_pool_size,
                # depending on which layer is the actual bottleneck).
                pool = getattr(deps, "pg_pool", None)
                if pool is not None:
                    try:
                        size = pool.get_size()
                        max_size = pool.get_max_size()
                        if max_size:
                            PG_POOL_SATURATION.set(size / max_size)
                            PG_POOL_SIZE.set(max_size)
                    except Exception:
                        # Some asyncpg minor versions rename these methods;
                        # never let metrics break the request path.
                        pass
            except ImportError:
                pass

    run_task = asyncio.create_task(_run_and_finalise())

    final: GeoRAGResponse | None = None
    try:
        while True:
            kind, payload = await status_queue.get()
            if kind == "status":
                yield await _stamped_event("status", {"message": payload})
            elif kind == "routing":
                # B1 — routing decision (classifier tier + model).
                # Laravel's StreamQueryFromFastApi can persist this to
                # query_audit_log.llm_model / sources_used.routing.
                yield await _stamped_event("routing", payload)
            elif kind == "bind":
                # Eval 02 follow-up (2026-05-20) — citations-bound-pre-tokens.
                # The orchestrator emits this AFTER evidence binding (Stage
                # 1) and BEFORE the LLM call streams the first delta. The
                # payload carries the full citation manifest the answer is
                # allowed to use. New chat clients render citation chips
                # immediately; legacy clients ignore the event and continue
                # to receive per-citation events after streaming (additive
                # migration — design doc in docs/proposals/).
                yield await _stamped_event("bind", payload)
            elif kind == "delta":
                # P0 #5 — real streamed token from the LLM. Bump token_count
                # so the Phase-3 fallback below doesn't re-synthesise deltas
                # from the final text after we already emitted them live.
                token_count += 1
                # P1 #16 — first-token latency. Observed exactly once per
                # request; the histogram is labelled by backend so we can
                # compare Anthropic streaming vs Ollama blocking.
                if not first_token_observed:
                    first_token_observed = True
                    try:
                        from app.metrics import FIRST_TOKEN_LATENCY  # noqa: PLC0415
                        FIRST_TOKEN_LATENCY.labels(
                            backend=settings.LLM_BACKEND
                        ).observe(_time_mod.monotonic() - stream_started)
                    except ImportError:
                        pass
                # M7B1: rename internal ``seq`` → ``token_seq`` so it's
                # distinct from the new event-level ``event_seq``.
                delta_data = {
                    "token": payload.get("token", ""),
                    "token_seq": payload.get("seq", 0),
                }
                yield await _stamped_event("delta", delta_data)
            elif kind == "done":
                final = payload  # type: ignore[assignment]
                break
            elif kind == "timeout":
                yield await _stamped_event(
                    "failed",
                    {
                        "error": "The query timed out. Try a more specific question.",
                        "code": "TIMEOUT",
                    },
                )
                return
            elif kind == "error":
                # Re-raise so the _guarded_stream wrapper in post_query()
                # can classify and emit a `failed` event with a proper
                # error code rather than a generic timeout.
                raise payload
    finally:
        # Ensure the background task is always awaited so its exceptions
        # are observed and the asyncio event loop doesn't warn about a
        # pending task at request teardown.
        if not run_task.done():
            run_task.cancel()
        try:
            await run_task
        except (asyncio.CancelledError, Exception):
            pass

    assert final is not None

    # Phase 3: if stream_text produced nothing, synthesise word-level deltas
    # from the final response text so the frontend always gets progressive
    # rendering rather than a single completed event.
    if token_count == 0 and final.text:
        # P1 #16 — observe first-token latency on the synth fallback too
        # so non-streaming backends (Ollama, Anthropic without
        # token_callback) still appear in the dashboard. Backend label
        # makes the streaming-vs-blocking comparison visible at a glance.
        if not first_token_observed:
            first_token_observed = True
            try:
                from app.metrics import FIRST_TOKEN_LATENCY  # noqa: PLC0415
                FIRST_TOKEN_LATENCY.labels(
                    backend=settings.LLM_BACKEND
                ).observe(_time_mod.monotonic() - stream_started)
            except ImportError:
                pass
        words = final.text.split(" ")
        for i, word in enumerate(words):
            chunk = word if i == len(words) - 1 else word + " "
            # M7B1: use token_seq for the per-token counter in synthetic deltas too.
            yield await _stamped_event("delta", {"token": chunk, "token_seq": i})
            # Yield control back to the event loop between each chunk so
            # FastAPI can flush the buffer to the client.
            await asyncio.sleep(0)

    # Phase 4: emit each citation so the frontend can render markers inline.
    for citation in final.citations:
        yield await _stamped_event("citation", citation.model_dump())

    # Phase 5: emit the completed event with the full GeoRAGResponse.
    yield await _stamped_event("completed", final.model_dump())

    logger.info(
        "agent_rag_stream: completed project=%s tokens=%d citations=%d confidence=%.2f",
        body.project_id,
        token_count,
        len(final.citations),
        final.confidence,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/queries",
    response_class=StreamingResponse,
    summary="Submit a geological RAG query",
    description=(
        "Accepts a natural-language query scoped to a project and streams the "
        "Pydantic AI agent response as Server-Sent Events. "
        "Requires X-Service-Key header authentication."
    ),
    dependencies=[Depends(verify_service_key)],
)
# Rate limit per (workspace_id, user_id) from the JWT — see
# app/services/rate_limit.py. No-op when settings.RATE_LIMIT_ENABLED=False.
# Default 20/minute (overridable via RATE_LIMIT_QUERIES env). Tighter than
# the global default because every call here triggers an LLM run.
@limiter.limit(settings.RATE_LIMIT_QUERIES)
async def post_query(
    body: QueryRequest,
    request: Request,
    user: UserContext = Depends(extract_user_context),
) -> StreamingResponse:
    """POST /internal/queries — stream a RAG response as SSE.

    The endpoint returns immediately with a streaming response; the generator
    runs in the background as FastAPI iterates it. If the generator raises an
    unhandled exception after the HTTP headers have been sent, a 'failed' event
    is appended so the client knows the stream terminated abnormally.
    """
    # P0 #4 — Multi-tenant project boundary enforcement.
    # Checked BEFORE opening the StreamingResponse so we can return a real
    # 403 instead of an SSE `failed` frame the client might miss. When the
    # flag is off we fall back to the soft-warn behaviour inside
    # _agent_rag_stream (graceful rollout — see config docstring).
    from app.config import settings as _settings  # noqa: PLC0415

    if _settings.MULTI_TENANT_ENFORCEMENT_ENABLED:
        if not user.project_id:
            logger.warning(
                "post_query: rejecting request with no JWT project_id claim "
                "(multi-tenant enforcement on); body_project=%s",
                body.project_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing project_id claim on JWT. Re-authenticate.",
            )
        if user.project_id != body.project_id:
            logger.warning(
                "post_query: rejecting cross-project request — "
                "jwt_project=%s body_project=%s user_id=%s",
                user.project_id,
                body.project_id,
                user.user_id,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="JWT project does not match request project.",
            )

    # M7B1 — Create a request-scoped EventStamper. The UUID is pre-generated
    # here rather than delegated to the orchestrator so the stamper is live
    # before the first keepalive frame. The DB answer_run_id (generated inside
    # run_deterministic_rag) is distinct from this streaming-session UUID; the
    # completed event carries the full GeoRAGResponse which Module 7 Chunk 4
    # will extend to surface the DB answer_run_id for cross-reference.
    _stream_run_id = uuid4()
    _stamper = EventStamper(answer_run_id=_stream_run_id)

    async def _guarded_stream() -> AsyncIterator[str]:
        """Wrap the agent stream with top-level error handling.

        Once StreamingResponse has started writing, we cannot change the HTTP
        status code. The 'failed' SSE event is the only error signal available
        to the client after the stream opens.
        """
        # Imports hoisted out of the except block so they don't add to
        # exception-handler latency. log_safe.query_hash + pricing.user_bucket
        # both ship with the app and never fail to import.
        from app.agent.errors import classify_error  # noqa: PLC0415
        from app.agent.log_safe import query_hash, project_tag  # noqa: PLC0415
        from app.agent.pricing import user_bucket  # noqa: PLC0415

        try:
            async for chunk in _agent_rag_stream(
                body, request.app.state, user=user, stamper=_stamper
            ):
                yield chunk
        except asyncio.CancelledError:
            # Client disconnected — log and stop silently.
            logger.info(
                "Query stream cancelled (client disconnected)",
                extra={
                    "project_id_tag": project_tag(body.project_id),
                    "query_hash": query_hash(body.query),
                },
            )
            raise
        except Exception as exc:
            # P1 #8 — rich exception logging. Replaces the prior log line
            # which (a) leaked the first 120 chars of query plaintext via
            # the `query` extra field — bypassing the P0 #3 redaction —
            # and (b) carried no Loki-friendly fields for grouping.
            #
            # The new extras are all low-cardinality + non-PII so they're
            # safe to ship to a shared Loki tenant:
            #   - query_hash         HMAC fingerprint (correlates to
            #                        query_audit_logs.query_text_hash)
            #   - project_id_tag     first 8 chars of UUID (one project
            #                        per customer install — not PII)
            #   - user_bucket        16-bucket hash (per pricing helper)
            #   - error_class        type(exc).__name__ for grouping
            #   - error_message      str(exc)[:200]
            error_code, user_message = classify_error(exc)
            logger.exception(
                "agent_rag_stream: unhandled exception (code=%s class=%s)",
                error_code.value,
                type(exc).__name__,
                extra={
                    "query_hash": query_hash(body.query),
                    "project_id_tag": project_tag(body.project_id),
                    "user_bucket": user_bucket(user.user_id if user else None),
                    "error_code": error_code.value,
                    "error_class": type(exc).__name__,
                    "error_message": str(exc)[:200],
                },
            )
            _redis_for_err = getattr(request.app.state, "redis_client", None)
            _failed_data: dict[str, Any] = {
                "error": user_message,
                "code": error_code.value,
            }
            _seq, _eid = _stamper.next()
            _failed_enriched: dict[str, Any] = {
                **_failed_data,
                "event_seq": _seq,
                "event_id": _eid,
                "answer_run_id": str(_stamper.answer_run_id),
                "trace_id": _stamper.trace_id,
                "event_name": "failed",
            }
            await _stamper.push_to_redis(_redis_for_err, "failed", _failed_enriched)
            yield _sse_event("failed", _failed_enriched)

    return StreamingResponse(
        _guarded_stream(),
        media_type="text/event-stream",
        headers={
            # Prevent intermediary proxies (nginx, Laravel Octane's reverse
            # proxy) from buffering the stream before it reaches the client.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            # Allow Laravel's EventSource client to reconnect automatically.
            "Connection": "keep-alive",
        },
    )
