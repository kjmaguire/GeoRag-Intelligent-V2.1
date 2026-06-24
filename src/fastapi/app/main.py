"""GeoRAG FastAPI domain service — application entry point.

Router registration
-------------------
All /internal/* routes live in dedicated router modules so each can be tested
in isolation. The prefix "/internal" is applied here rather than inside each
router so the router modules stay prefix-agnostic and reusable.

Lifespan
--------
Shared resources (database pools, embedding model) are initialised in the
lifespan context manager so they are ready before the first request and
cleanly torn down on shutdown. Each pool is stored on app.state so route
handlers and agent tools can access it via request.app.state.

Pool storage on app.state
-------------------------
  app.state.pg_pool          — asyncpg.Pool (PostGIS via PgBouncer)
  app.state.qdrant_client    — AsyncQdrantClient
  app.state.neo4j_driver     — neo4j.AsyncDriver
  app.state.redis_client     — redis.asyncio.Redis
  app.state.anthropic_client — anthropic.AsyncAnthropic | None (B2 — pooled
                               to avoid TLS handshake + pool churn per request;
                               None if LLM_BACKEND != "anthropic" or key unset)
  app.state.embedding_model  — SentenceTransformer (BAAI/bge-small-en-v1.5, CPU)
  app.state.reranker         — CrossEncoder (cross-encoder/ms-marco-MiniLM-L-6-v2, CPU)

Timeout constants are imported from app.config.settings so every module
reading them gets the same validated value.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import asyncpg
import redis.asyncio as aioredis
import sentry_sdk
from fastapi import FastAPI, HTTPException
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient
from starlette.responses import Response  # for /metrics return-type resolution

from app.config import settings
from app.logging_config import configure_json_logging

# Sentry — initialised at import time so import-side errors are captured.
# The FastAPI / Starlette / asyncpg / redis / httpx integrations are
# auto-detected from installed packages; no explicit registration needed.
# Gated on SENTRY_DSN being non-empty so a blank DSN safely disables the SDK.
if settings.SENTRY_DSN:
    # Explicit integration list — Sentry auto-enables most of these via
    # `auto_enabling_integrations` but we name the high-leverage ones so a
    # future SDK bump can't quietly drop coverage on the agent path. Each
    # one auto-instruments its package when imported.
    from sentry_sdk.integrations.anthropic import AnthropicIntegration
    from sentry_sdk.integrations.pydantic_ai import PydanticAIIntegration

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.SENTRY_ENVIRONMENT,
        release=settings.SENTRY_RELEASE or None,
        traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
        profiles_sample_rate=settings.SENTRY_PROFILES_SAMPLE_RATE,
        send_default_pii=True,
        # Forward Python `logging` records into Sentry's Logs product.
        # Pairs with the Laravel side's sentry_logs Monolog channel.
        _experiments={"enable_logs": settings.SENTRY_ENABLE_LOGS},
        integrations=[
            # Anthropic streaming spans — captures messages.create and
            # messages.stream calls including token usage, model id,
            # service tier, and tool-use blocks.
            AnthropicIntegration(include_prompts=False),
            # pydantic-ai agent + tool spans — every @agent.tool invocation,
            # output_validator pass, and run_stream is wrapped as a span,
            # giving us a full trace of orchestrator.py's agentic path
            # without manual breadcrumbs.
            PydanticAIIntegration(include_prompts=False),
        ],
    )
from app.routers import answer_runs as answer_runs_router
from app.routers import evidence as evidence_router
from app.routers import exports as exports_router
from app.routers import outlier_assist as outlier_assist_router
from app.routers import pdf as pdf_router
from app.routers import projects, queries
from app.routers import phase0_ops as phase0_ops_router
from app.routers import shadow_trigger as shadow_trigger_router
from app.routers import metrics_ingestion_events as metrics_ingestion_events_router
from app.routers import mv_refresh_trigger as mv_refresh_trigger_router
from app.routers import integrations_trigger as integrations_trigger_router
from app.routers import ocr_render as ocr_render_router
from app.routers import re_ocr_trigger as re_ocr_trigger_router
from app.routers import support_agents as support_agents_router  # Phase G.5 follow-up
from app.routers import visualizations as visualizations_router  # Phase H4 §5
from app.routers import target_recommendation_cockpit as trg_cockpit_router  # Phase H4 §8 UI
from app.routers import report_builder as report_builder_router  # Phase H4 §7 UI
from app.routers import ml_training as ml_training_router  # Phase H4 §12 UI
from app.routers import citation_feedback as citation_feedback_router  # Phase H4 §12.8 UI
from app.routers import conflicts as conflicts_router  # Phase H4 §7.4 UI
from app.routers import audit_findings as audit_findings_router  # Phase H4 §11.5/11.10/6.4 UI
from app.routers import what_changed as what_changed_router  # Phase H4 §9.9 UI
from app.routers import admin_tier1_misc as tier1_misc_router  # Phase H4 Tier 1 — source-trust + export-gate + k6
from app.routers import admin_tier234 as tier234_router  # Phase H4 Tier 2/3/4 — recommendations + QP + members + settings + AP + audit + maps
from app.routers import assessment_summary as assessment_summary_router  # CC-01 Item 5 — assessment report structured summary
from app.routers import maps as maps_router  # CC-01 Item 3 (stub) — map ingest scaffold
from app.routers import coverage as coverage_router  # CC-03 Item 5 — coverage density heatmap
from app.routers import completeness as completeness_router  # CC-03 Item 2 — completeness audit
from app.routers import smdi as smdi_router  # SMDI ingestion plan v1.1 Phase 6 — features endpoint

# V1.5-05 — switch to JSON logs at module import so every logger.info() in
# the app emits a structured payload Promtail can ingest with a single
# `| json` pipeline stage. Pairs with V1.5-04 on the Laravel side.
configure_json_logging(level=settings.LOG_LEVEL.upper())

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — pool initialisation and teardown
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise all shared database pools and clients before first request.

    Resources are stored on ``app.state`` so any route handler or agent tool
    can retrieve them via ``request.app.state.<resource>`` without module-level
    globals.

    Startup sequence:
      1. asyncpg connection pool → PostGIS via PgBouncer
      2. AsyncQdrantClient → Qdrant vector store
      3. Neo4j AsyncDriver → knowledge graph
      4. redis.asyncio client → caching / session store
      5. SentenceTransformer embedding model (BAAI/bge-small-en-v1.5, CPU)
      6. CrossEncoder reranker (cross-encoder/ms-marco-MiniLM-L-6-v2, CPU)

    Teardown is the mirror: each client is closed in reverse order so
    in-flight requests can complete before their pools disappear.
    """
    # -------------------------------------------------------------------------
    # 0. Logfire / OpenTelemetry — instrument BEFORE other resources so spans
    #    wrap pool creation, the embedding-model warmup, and the first request.
    #    See settings.LOGFIRE_* and the SECURITY.md "Observability" section
    #    for the rollout recipe.
    # -------------------------------------------------------------------------
    if settings.LOGFIRE_ENABLED:
        try:
            import logfire  # noqa: PLC0415

            # Decide where spans go. Order matters: hosted backend wins,
            # then OTLP collector, then in-process-only (debug mode).
            if settings.LOGFIRE_TOKEN:
                logfire.configure(
                    token=settings.LOGFIRE_TOKEN,
                    service_name=settings.LOGFIRE_SERVICE_NAME,
                    environment=settings.LOGFIRE_ENVIRONMENT,
                    send_to_logfire=True,
                )
                logger.info(
                    "Logfire configured (hosted backend, service=%s env=%s)",
                    settings.LOGFIRE_SERVICE_NAME,
                    settings.LOGFIRE_ENVIRONMENT,
                )
            elif settings.LOGFIRE_OTEL_ENDPOINT:
                # Logfire defers to OTel exporter env vars when
                # send_to_logfire=False — set them here so the operator
                # only has to set LOGFIRE_OTEL_ENDPOINT in .env.
                import os  # noqa: PLC0415
                os.environ.setdefault(
                    "OTEL_EXPORTER_OTLP_ENDPOINT",
                    settings.LOGFIRE_OTEL_ENDPOINT,
                )
                logfire.configure(
                    service_name=settings.LOGFIRE_SERVICE_NAME,
                    environment=settings.LOGFIRE_ENVIRONMENT,
                    send_to_logfire=False,
                )
                logger.info(
                    "Logfire configured (OTLP → %s, service=%s env=%s)",
                    settings.LOGFIRE_OTEL_ENDPOINT,
                    settings.LOGFIRE_SERVICE_NAME,
                    settings.LOGFIRE_ENVIRONMENT,
                )
            else:
                logfire.configure(
                    service_name=settings.LOGFIRE_SERVICE_NAME,
                    environment=settings.LOGFIRE_ENVIRONMENT,
                    send_to_logfire=False,
                )
                logger.warning(
                    "Logfire configured in LOCAL-ONLY mode "
                    "(neither LOGFIRE_TOKEN nor LOGFIRE_OTEL_ENDPOINT set)"
                )

            # Wire the four instrumentations the Pydantic team ships.
            # `instrument_pydantic_ai` is the headline win — every agent.run
            # produces a span with system prompt, tool calls, retries, and
            # the final output. The other three add HTTP/DB span context so
            # latency attribution is end-to-end.
            try:
                logfire.instrument_pydantic_ai()
            except Exception:
                logger.debug("logfire.instrument_pydantic_ai failed", exc_info=True)
            try:
                logfire.instrument_fastapi(app, capture_headers=False)
            except Exception:
                logger.debug("logfire.instrument_fastapi failed", exc_info=True)
            try:
                logfire.instrument_asyncpg()
            except Exception:
                logger.debug("logfire.instrument_asyncpg failed", exc_info=True)
            try:
                logfire.instrument_httpx()
            except Exception:
                logger.debug("logfire.instrument_httpx failed", exc_info=True)
        except Exception:
            # Logfire failure must never block startup — observability is
            # additive. The exception is exception()-logged so the operator
            # sees the stack trace but the app continues.
            logger.exception(
                "Logfire init failed — proceeding without OTel instrumentation"
            )
    else:
        logger.info("Logfire disabled (LOGFIRE_ENABLED=false)")

    # -------------------------------------------------------------------------
    # 1. asyncpg connection pool (PostGIS via PgBouncer)
    # -------------------------------------------------------------------------
    pg_dsn = (
        f"postgresql://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}"
        f"@{settings.POSTGRES_HOST}:{settings.POSTGRES_PORT}/{settings.POSTGRES_DB}"
    )
    logger.info(
        "Connecting asyncpg pool -> %s:%s/%s",
        settings.POSTGRES_HOST,
        settings.POSTGRES_PORT,
        settings.POSTGRES_DB,
    )
    pg_pool: asyncpg.Pool = await asyncpg.create_pool(
        dsn=pg_dsn,
        min_size=2,
        # FastAPI review #4 — per-worker max trimmed from 25 → 12 because
        # the Dockerfile runs 4 uvicorn workers. Total DB connection
        # ceiling is now 4 × 12 = 48, which fits within PgBouncer's
        # `default_pool_size=100` (raised in compose at the same time as
        # this change). The original 25 × 4 = 100 was at the PgBouncer
        # ceiling with zero headroom, causing acquire timeouts under load.
        # If you re-flatten to a single uvicorn worker (review item #4
        # option B), bump this back to 25.
        max_size=12,
        # command_timeout matches the PostGIS per-query timeout from Section 06e.
        # PgBouncer's server_idle_timeout is set in the PgBouncer config; we
        # set max_inactive_connection_lifetime slightly below it to avoid
        # receiving a connection that PgBouncer has already closed.
        command_timeout=settings.TIMEOUT_POSTGIS_S,
        max_inactive_connection_lifetime=270.0,
        # DB review (Critical #1) — PgBouncer is in transaction-pool mode,
        # which rotates Postgres backend connections per transaction. asyncpg's
        # default is to use protocol-level PREPARE statements, which PgBouncer
        # then tries to re-use on a different backend connection — this throws
        # `InvalidSQLStatementNameError: prepared statement "__asyncpg_stmt_N__"
        # does not exist` under concurrent load. Disabling the statement cache
        # makes asyncpg send queries via the simple protocol (one-shot parse),
        # which is fully compatible with transaction pooling. The per-query
        # parse cost is ~100 µs, dwarfed by network + PostGIS time.
        statement_cache_size=0,
        server_settings={
            # Visible in pg_stat_activity.application_name for triage.
            "application_name": "georag-fastapi",
            # DB review (Critical #3) — JIT adds 30–80 ms of LLVM compile
            # overhead on first-plan and is a net loss for OLTP RAG tool
            # queries (<10 ms expected). We also set -c jit=off in the
            # Postgres command line; this belt-and-braces guard ensures the
            # setting survives any future ALTER DATABASE meddling.
            "jit": "off",
            # NOTE — statement_timeout is NOT set here. PgBouncer 1.25's
            # ignore_startup_parameters silently drops the value instead of
            # forwarding it to Postgres (track_extra_parameters is needed
            # but the edoburu image doesn't expose it as an env var).
            # Instead, statement_timeout is set per-transaction via
            # `AgentDeps.acquire_scoped`, which both:
            #   1. Opens a transaction (safe under PgBouncer transaction
            #      pooling — asyncpg holds the backend until COMMIT).
            #   2. Issues SET LOCAL statement_timeout = '10s'
            #      AND SET LOCAL georag.project_id = '<uuid>'
            # Tools that still acquire a raw connection from pg_pool do not
            # get the timeout. Migrating every tool to acquire_scoped is the
            # single rollout for both runaway-query protection and the
            # multi-tenant RLS path.
        },
    )
    app.state.pg_pool = pg_pool
    logger.info(
        "asyncpg pool ready (min=4 max=25, statement_cache_size=0, jit=off)"
    )

    # -------------------------------------------------------------------------
    # P0 #4 — visible startup banner for multi-tenant RBAC posture
    # -------------------------------------------------------------------------
    if settings.MULTI_TENANT_ENFORCEMENT_ENABLED:
        logger.info(
            "RBAC: MULTI_TENANT_ENFORCEMENT_ENABLED=True — requests without "
            "a valid JWT project_id matching the request body will be rejected "
            "with HTTP 403."
        )
    else:
        logger.warning(
            "RBAC: MULTI_TENANT_ENFORCEMENT_ENABLED=False — this deployment is "
            "running in single-tenant / graceful-rollout mode. JWT project_id "
            "mismatches are logged as warnings but NOT rejected. Do not deploy "
            "in a multi-customer environment until this flag is set to True."
        )

    # -------------------------------------------------------------------------
    # 2. Async Qdrant client
    # -------------------------------------------------------------------------
    logger.info("Connecting Qdrant client -> %s:%s", settings.QDRANT_HOST, settings.QDRANT_PORT)
    qdrant_client = AsyncQdrantClient(
        host=settings.QDRANT_HOST,
        port=settings.QDRANT_PORT,
        # Qdrant review #9 — optional API key for prod. Empty string in
        # single-tenant dev posture (network isolation handles auth) —
        # the qdrant-client lib treats "" as "no auth header", which is
        # exactly what we want when the Qdrant container's own
        # QDRANT__SERVICE__API_KEY is also empty.
        api_key=settings.QDRANT_API_KEY or None,
        timeout=int(settings.TIMEOUT_QDRANT_S),
        check_compatibility=False,  # avoids a blocking HTTP call at startup
    )
    app.state.qdrant_client = qdrant_client
    logger.info(
        "Qdrant client ready (api_key_set=%s)",
        bool(settings.QDRANT_API_KEY),
    )

    # -------------------------------------------------------------------------
    # 3. Neo4j async driver
    # -------------------------------------------------------------------------
    neo4j_uri = f"bolt://{settings.NEO4J_HOST}:{settings.NEO4J_PORT}"
    logger.info("Connecting Neo4j driver -> %s", neo4j_uri)
    neo4j_driver = AsyncGraphDatabase.driver(
        neo4j_uri,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        # FastAPI review #4 — per-worker max trimmed from 25 → 12.
        # 4 uvicorn workers × 12 = 48 client-side total, which fits
        # under Neo4j's `server.bolt.thread_pool_max_size=50` server
        # ceiling. The previous 25 × 4 = 100 was double the server slot
        # count — caused intermittent `Acquiring connection from pool
        # timed out` errors from Neo4j under load. If you re-flatten to
        # a single uvicorn worker, bump this back to 25.
        max_connection_pool_size=12,
        # Fail fast under pool contention rather than waiting the
        # default 60 s — at that point the request has long since
        # blown its overall deadline.
        connection_acquisition_timeout=5.0,
        # Interactive chat path doesn't tolerate the default 30 s
        # transient-error retry. A failed transient (network blip,
        # leader election) should bubble up to the FastAPI tool wrapper
        # well within the per-tool 3 s ceiling.
        max_transaction_retry_time=5.0,
        # connection_timeout maps to the Section 06e Neo4j timeout;
        # individual QUERY timeouts are now enforced via the per-call
        # `timeout` kwarg on session.run() (Neo4j review #5) instead
        # of the cluster-wide db.transaction.timeout setting.
        connection_timeout=settings.TIMEOUT_NEO4J_S,
    )
    app.state.neo4j_driver = neo4j_driver
    logger.info(
        "Neo4j driver ready (max_pool=25, acquire_timeout=5s, retry=5s)"
    )

    # -------------------------------------------------------------------------
    # 4. Redis async client
    # -------------------------------------------------------------------------
    logger.info("Connecting Redis client -> %s:%s", settings.REDIS_HOST, settings.REDIS_PORT)
    redis_client = aioredis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None,
        socket_timeout=settings.TIMEOUT_REDIS_S,
        socket_connect_timeout=settings.TIMEOUT_REDIS_S,
        decode_responses=True,
        # RESP3 protocol (redis-py 7+ / Redis 8). Richer return types
        # (maps, sets, bools natively) and the prerequisite if/when
        # redis-py adds async client-side caching (currently sync-only;
        # see https://github.com/redis/redis-py — `cache_config=` lands
        # on `redis.Redis()` first). Old RESP2 servers fall through to
        # RESP2 negotiation, so this is safe across upgrades.
        protocol=3,
        # Redis review #4 — pool + health hardening.
        # `max_connections` caps per-worker connections so 4 uvicorn
        # workers × unbounded pool can't push past Redis's maxclients.
        # 32 per worker × 4 workers = 128, well under the 10000 ceiling
        # and comfortable for the agent's cache-heavy paths.
        max_connections=32,
        # `health_check_interval=30` sends a periodic PING on idle
        # connections so we detect dead sockets on the next checkout
        # instead of paying the timeout on a real query.
        health_check_interval=30,
        # `client_name` tags every connection in `CLIENT LIST` so
        # operators can tell FastAPI connections apart from Laravel /
        # Horizon / Reverb during triage.
        client_name="georag-fastapi",
        # Dedicated db for FastAPI cache — keeps the chat-response
        # cache from colliding with Laravel's db0/db1 keys and lets
        # operators run FLUSHDB safely on just this db during triage.
        db=2,
    )
    app.state.redis_client = redis_client
    logger.info(
        "Redis client ready (db=2, max_connections=32, client_name=georag-fastapi)"
    )

    # -------------------------------------------------------------------------
    # 4a-bis. Register the agent runtime (Phase 5 follow-up, 2026-05-19)
    # -------------------------------------------------------------------------
    # Until now, `register_runtime(pg_pool, redis)` was only called from the
    # Hatchet AI worker (`hatchet_workflows/phase0_agents.py`). That meant
    # any FastAPI-direct invocation of an `@georag_agent`-decorated function
    # — e.g. `POST /api/v1/incidents/diagnose`, `POST /api/v1/support/packets/assemble`
    # — failed with `RuntimeError: agents.runtime not registered`. Surfaced
    # during the Phase 5 quality eval (smoke test of llm_incident_diagnosis).
    # Now the same runtime is registered here so agent HTTP endpoints work
    # in addition to the Hatchet path. Idempotent: register_runtime() is
    # safe to call twice — the second call just overwrites the singleton.
    try:
        from app.agents import register_runtime  # noqa: PLC0415

        register_runtime(pg_pool=pg_pool, redis=redis_client)
        logger.info("Agent runtime registered (FastAPI lifespan)")
    except Exception:
        logger.exception(
            "Failed to register agent runtime — agent HTTP endpoints "
            "(/api/v1/incidents/diagnose, etc.) will return 500"
        )

    # -------------------------------------------------------------------------
    # 4a-tris. Langfuse client (Phase 5 follow-up, 2026-05-19)
    # -------------------------------------------------------------------------
    # Until now, Langfuse env vars + keys were configured (LANGFUSE_HOST,
    # _PUBLIC_KEY, _SECRET_KEY) but the SDK was never instantiated and no
    # orchestrator code emitted traces. The infrastructure was dark.
    # Initialise here so the singleton is shared across the orchestrator;
    # `_call_openai_compatible_llm` emits a `generation` observation per
    # LLM call (minimum-viable instrumentation; per-step tracing can come
    # later). Initialisation is lazy + failure-tolerant — missing env, no
    # network reachability, or a bad key only logs at warn and leaves
    # langfuse_client=None, which the call sites no-op around.
    app.state.langfuse_client = None
    if (
        settings.LANGFUSE_PUBLIC_KEY
        and settings.LANGFUSE_SECRET_KEY
        and settings.LANGFUSE_HOST
    ):
        try:
            # SDK quirk: the modern Langfuse SDK reads BOTH LANGFUSE_BASE_URL
            # (browser-facing) AND LANGFUSE_HOST from env, with BASE_URL
            # taking precedence in the OTel exporter even when `host=` is
            # passed to the constructor. .env keeps BASE_URL set to
            # localhost:3001 (for the support-cockpit "open in Langfuse"
            # deep-links from the host browser), but inside the container
            # localhost:3001 is unreachable — exports fail with "Connection
            # refused". Override the env in-process so the SDK transport
            # uses the in-network hostname. The original BASE_URL is still
            # read elsewhere via `settings.LANGFUSE_BASE_URL` (Pydantic
            # captured it at import time) if any code path needs it.
            import os as _os  # noqa: PLC0415

            _os.environ["LANGFUSE_BASE_URL"] = settings.LANGFUSE_HOST

            from langfuse import Langfuse  # noqa: PLC0415

            app.state.langfuse_client = Langfuse(
                public_key=settings.LANGFUSE_PUBLIC_KEY,
                secret_key=settings.LANGFUSE_SECRET_KEY,
                host=settings.LANGFUSE_HOST,
                tracing_enabled=True,
                # Small flush window so smoke tests see traces quickly.
                # Production may want a larger interval for batching.
                flush_interval=2.0,
            )
            logger.info(
                "Langfuse client ready (host=%s, public_key=%s...)",
                settings.LANGFUSE_HOST,
                (settings.LANGFUSE_PUBLIC_KEY or "")[:8],
            )
        except Exception:
            logger.exception(
                "Failed to initialise Langfuse client — RAG traces "
                "will not be recorded. App continues without observability."
            )

    # -------------------------------------------------------------------------
    # 4b. Anthropic async client (B2 — pool once at startup)
    # -------------------------------------------------------------------------
    # Pre-A5: orchestrator constructed a fresh AsyncAnthropic per call, paying
    # a TLS handshake + HTTP/2 stream-setup cost each time. Now pooled once.
    # Only initialised when LLM_BACKEND=anthropic and the key is set; otherwise
    # left as None and the orchestrator falls back to lazy construction (a
    # grace path for transitional deploys).
    app.state.anthropic_client = None
    if settings.LLM_BACKEND == "anthropic" and settings.ANTHROPIC_API_KEY:
        try:
            from anthropic import AsyncAnthropic  # noqa: PLC0415

            app.state.anthropic_client = AsyncAnthropic(
                api_key=settings.ANTHROPIC_API_KEY,
            )
            logger.info("Anthropic client ready (pooled)")
        except Exception:
            logger.exception(
                "Failed to pool AsyncAnthropic at startup — orchestrator will "
                "fall back to per-call construction"
            )

    # -------------------------------------------------------------------------
    # 4c. OpenAI-compatible httpx client (P1 #13 — pool once at startup)
    # -------------------------------------------------------------------------
    # Mirrors the Anthropic pool above for the vLLM path. Previously
    # `_call_openai_compatible_llm` constructed `async with httpx.AsyncClient`
    # per call — paying TLS handshake + connection-pool warmup every time,
    # adding 30-100 ms to each call. With a pooled client the connection
    # is kept-alive across requests.
    #
    # Always initialised — even on Anthropic deploys we keep this around
    # because the local-LLM cross-backend failover path also goes through
    # this client when Anthropic 429s. The orchestrator falls back to ad-hoc
    # construction if app.state lookup fails (test path).
    import httpx  # noqa: PLC0415

    # http2=True only when h2 is installed AND the target endpoint speaks it.
    # Pool sized for the same 25-concurrency ceiling as asyncpg so the LLM
    # stage can never become the bottleneck before the DB does.
    app.state.openai_http_client = httpx.AsyncClient(
        timeout=settings.TIMEOUT_GATHER_S,
        limits=httpx.Limits(
            max_connections=25,
            max_keepalive_connections=10,
            keepalive_expiry=settings.TIMEOUT_GATHER_S * 2,
        ),
    )
    logger.info(
        "OpenAI-compatible httpx client ready (pooled, max_connections=25)"
    )

    # -------------------------------------------------------------------------
    # 5. SentenceTransformer embedding model — bge-small-en-v1.5
    # -------------------------------------------------------------------------
    # BAAI/bge-small-en-v1.5 replaces all-MiniLM-L6-v2 (same 384-dim cosine,
    # no collection recreation needed).  BGE scores 10-15% higher than MiniLM
    # on mineral/geological queries.  The model runs on CPU — the FastAPI
    # container has no GPU passthrough.  A single encode() call per request
    # (query embedding only) is fast enough at CPU speeds; batch document
    # indexing runs in the Dagster container via scripts/reembed_qdrant.py.
    logger.info("Loading embedding model: %s", settings.EMBEDDING_MODEL_NAME)
    _t0 = time.perf_counter()
    try:
        # Shared embedding sidecar when EMBEDDING_SERVICE_URL is set (one model
        # for all workers over a localhost hop); else a local CPU model as
        # before. See app.services.embedding.
        from app.services.embedding import get_embedding_model  # noqa: PLC0415

        embedding_model = get_embedding_model(settings.EMBEDDING_MODEL_NAME)
        # Warm up: encode a dummy string so the first real request does not
        # pay the JIT/model-init penalty (a no-op round-trip for the sidecar
        # proxy, which also validates connectivity at startup).
        embedding_model.encode("warm-up", normalize_embeddings=True)
        _elapsed = time.perf_counter() - _t0
        app.state.embedding_model = embedding_model
        logger.info(
            "Embedding model ready: %s (dim=%d) loaded in %.2fs",
            settings.EMBEDDING_MODEL_NAME,
            embedding_model.get_sentence_embedding_dimension(),
            _elapsed,
        )
    except Exception:
        logger.exception(
            "Failed to load embedding model — search_documents will return empty results"
        )
        app.state.embedding_model = None

    # -------------------------------------------------------------------------
    # 6. Cross-encoder reranker — BAAI/bge-reranker-base (Module 4 Chunk 3)
    # -------------------------------------------------------------------------
    # bge-reranker-base (Apache 2.0, ~278 MB) replaces ms-marco-MiniLM-L-6-v2.
    # It is pinned by HuggingFace revision SHA (see reranker.py) so weight
    # drift is detected via the version string in answer_runs.reranker_version.
    #
    # The reranker now runs on the FUSED candidate set (post cross-store RRF),
    # not just on Qdrant-only results. Per-class top-k is defined in
    # app.services.reranker.RERANKER_TOP_K_BY_CLASS.
    #
    # Fallback policy (spec B6): if the reranker fails to load or predict,
    # log + continue with RRF order. Do not fail the query.
    _t1 = time.perf_counter()
    try:
        import os as _os  # noqa: PLC0415
        from app.services.reranker import (  # noqa: PLC0415
            RERANKER_VERSION,
            _RemoteReranker,
            _get_reranker,
        )

        # 2026-06-24: when the shared reranker sidecar is configured
        # (RERANKER_SERVICE_URL), point app.state.reranker at an HTTP proxy
        # instead of loading a per-worker CrossEncoder copy. 6 uvicorn workers
        # each loading a ~1 GiB model was the OOM driver; the proxy keeps the
        # identical .predict() interface the query path already uses.
        _svc_url = (_os.environ.get("RERANKER_SERVICE_URL") or "").strip()
        if _svc_url:
            _timeout = float(_os.environ.get("RERANKER_SERVICE_TIMEOUT_S", "10"))
            app.state.reranker = _RemoteReranker(_svc_url, _timeout)
            app.state.reranker_version = RERANKER_VERSION
            logger.info(
                "Reranker via shared sidecar %s (%s) — no local model loaded",
                _svc_url, RERANKER_VERSION,
            )
        else:
            logger.info(
                "Loading cross-encoder reranker: %s version=%s",
                "BAAI/bge-reranker-base",
                RERANKER_VERSION,
            )
            reranker = _get_reranker()  # warms the lru_cache singleton
            _elapsed_r = time.perf_counter() - _t1
            app.state.reranker = reranker
            app.state.reranker_version = RERANKER_VERSION
            logger.info(
                "Reranker model ready: %s loaded in %.2fs",
                RERANKER_VERSION,
                _elapsed_r,
            )
    except Exception:
        logger.exception(
            "Failed to load reranker model — reranker step will be skipped (RRF order used)"
        )
        app.state.reranker = None
        app.state.reranker_version = None

    # -------------------------------------------------------------------------
    # 7. SPLADE++ sparse encoder pre-warm (Module 4 Chunk 2)
    # -------------------------------------------------------------------------
    # The lru_cache-backed _get_sparse_model() loads the model on first call.
    # Pre-warming here ensures the ~440 MB model is resident before the first
    # RAG request instead of adding 3-10s latency to the first query.
    # Each Uvicorn worker process runs this independently (4 workers = 4x load).
    logger.info("Pre-warming SPLADE++ sparse encoder...")
    _t_sparse = time.perf_counter()
    try:
        from app.services.sparse_encoder import encode_sparse  # noqa: PLC0415

        _warmup_sparse = encode_sparse("drillhole uranium grade intercept")
        _elapsed_sparse = time.perf_counter() - _t_sparse
        logger.info(
            "SPLADE++ sparse encoder ready: %d non-zero terms, loaded in %.2fs",
            len(_warmup_sparse),
            _elapsed_sparse,
        )
    except Exception:
        logger.exception(
            "SPLADE++ encoder pre-warm failed -- hybrid retrieval will fail on "
            "first query. Install transformers and torch in pyproject.toml and rebuild."
        )

    # -------------------------------------------------------------------------
    # 8. §04p PDF Ingestion Subsystem — Stage 2 render service + Bronze store
    # -------------------------------------------------------------------------
    # PdfRenderService holds a ProcessPoolExecutor (process workers, not threads,
    # per §04p Stage 2 PDFium thread-safety requirement) and an LRU render cache.
    # LocalFsBronzeStore is a Phase 1.A stub; replace with SeaweedFsBronzeStore
    # in a follow-up phase when the S3-compatible API is integrated.
    try:
        from app.services.bronze_store import LocalFsBronzeStore  # noqa: PLC0415
        from app.services.pdf_render import PdfRenderService  # noqa: PLC0415

        app.state.pdf_render_service = PdfRenderService()
        app.state.bronze_store = LocalFsBronzeStore()
        logger.info("PDF render service and Bronze store ready (§04p Phase 1.A)")
    except Exception:
        logger.exception(
            "§04p PDF subsystem init failed — /pdf/* endpoints will return 503. "
            "Ensure pikepdf and pypdfium2 are installed: uv pip install 'pikepdf>=9.0' 'pypdfium2>=4.30'"
        )
        app.state.pdf_render_service = None
        app.state.bronze_store = None

    # -------------------------------------------------------------------------
    # 9. §04p PDF Ingestion Subsystem — Stage 3 extract service (Phase 1.B)
    # -------------------------------------------------------------------------
    # PdfExtractService holds a dedicated ProcessPoolExecutor (separate from
    # the render pool) for pdfminer.six / pdfplumber extraction.  Results are
    # cached durably in silver.pdf_text_blocks + silver.pdf_table_cells so
    # cross-process and cross-restart cache hits work.
    # The pool is initialised AFTER pg_pool (step 1) because the extract service
    # takes the pool reference at construction time.
    try:
        from app.services.pdf_extract import PdfExtractService  # noqa: PLC0415

        app.state.pdf_extract_service = PdfExtractService(pool=pg_pool)
        logger.info("PDF extract service ready (§04p Phase 1.B — pdfminer.six + pdfplumber)")
    except Exception:
        logger.exception(
            "§04p Phase 1.B extract service init failed — "
            "/pdf/extract_text and /pdf/find_tables will return 503. "
            "Ensure pdfminer.six and pdfplumber are installed: "
            "uv pip install 'pdfminer.six>=20240706' 'pdfplumber>=0.11'"
        )
        app.state.pdf_extract_service = None

    # -------------------------------------------------------------------------
    # 10. §04p PDF Ingestion Subsystem — Stage 4 layout service (Phase 1.C-i)
    # -------------------------------------------------------------------------
    # PdfLayoutService holds a dedicated ProcessPoolExecutor (separate from
    # the render + extract pools) for Docling document conversion.  Docling's
    # first inference call is heavy (ONNX model load); process isolation
    # prevents it from starving lighter workers under concurrent load.
    # Results are cached durably in silver.pdf_layout_regions.
    #
    # Defensive try/except: if docling is not installed the service is set to
    # None and /pdf/find_legends returns 503.  The rest of the app is unaffected.
    try:
        from app.services.pdf_layout import PdfLayoutService  # noqa: PLC0415

        app.state.pdf_layout_service = PdfLayoutService(pool=pg_pool)
        logger.info("PDF layout service ready (§04p Phase 1.C-i — Docling)")
    except Exception:
        logger.exception(
            "§04p Phase 1.C-i layout service init failed — "
            "/pdf/find_legends will return 503. "
            "Ensure docling is installed: uv pip install 'docling>=2.13'"
        )
        app.state.pdf_layout_service = None

    # -------------------------------------------------------------------------
    # 11. §04p PDF Ingestion Subsystem — Stage 5 OCR service (Phase 1.C-ii)
    # -------------------------------------------------------------------------
    # PdfOcrService holds a dedicated ProcessPoolExecutor (separate from the
    # render, extract, and layout pools) for PaddleOCR PP-OCRv5 calls.
    # PaddleOCR's first inference is heavy (model load + PaddlePaddle JIT) and
    # benefits from process isolation so it cannot starve lighter workers.
    # Results are cached durably in silver.pdf_ocr_results.
    #
    # Defensive try/except: if paddleocr or paddlepaddle is not installed the
    # service is set to None and /pdf/ocr_region returns 503.  The rest of the
    # app is unaffected.
    #
    # NOTE for operator: PaddlePaddle is heavy (~500 MB CPU-only wheel).
    # Run `uv pip install 'paddlepaddle>=3.1' 'paddleocr>=2.10'` or rebuild
    # the fastapi Docker image before using the /pdf/ocr_region endpoint.
    # Both packages are Apache 2.0 — clean per §04p license-posture table.
    app.state.pdf_ocr_service = None
    _render_svc = getattr(app.state, "pdf_render_service", None)
    if _render_svc is not None:
        try:
            from app.services.pdf_ocr import PdfOcrService  # noqa: PLC0415

            app.state.pdf_ocr_service = PdfOcrService(
                pool=pg_pool,
                render_service=_render_svc,
            )
            logger.info("PDF OCR service ready (§04p Phase 1.C-ii — PaddleOCR PP-OCRv5)")
        except Exception:
            logger.exception(
                "§04p Phase 1.C-ii OCR service init failed — "
                "/pdf/ocr_region will return 503. "
                "Ensure paddlepaddle and paddleocr are installed: "
                "uv pip install 'paddlepaddle>=3.1' 'paddleocr>=2.10'"
            )
    else:
        logger.warning(
            "§04p Phase 1.C-ii OCR service skipped — pdf_render_service is None. "
            "Render service must initialise successfully before the OCR service."
        )

    # -------------------------------------------------------------------------
    # 12. §04p PDF Ingestion Subsystem — Stage 6 VL service (Phase 1.D)
    # -------------------------------------------------------------------------
    # PdfVlService is async-native (httpx I/O to vLLM — no process pool).
    # It holds an asyncpg pool reference, a reference to PdfRenderService for
    # 200-DPI page renders, and an optional pooled httpx client.
    #
    # Config (all optional — defaults target the in-network vllm service):
    #   PDF_VL_MODEL_ID    — model identifier (default: "Qwen/Qwen2.5-VL-7B-Instruct")
    #   PDF_VL_BACKEND     — "vllm" | "anthropic" (default: "vllm")
    #   PDF_VL_BACKEND_URL — full base URL (default: "http://vllm:8000/v1")
    #   PDF_VL_TIMEOUT_S   — inference timeout in seconds (default: 120)
    #   PDF_VL_MAX_PAGES   — max pages per request (default: 4)
    #
    # OPERATOR ACTION REQUIRED before /pdf/summarize_section works:
    #   vLLM serves: --model Qwen/Qwen2.5-VL-7B-Instruct on /v1
    #   All Python deps are already present (httpx + asyncpg + pydantic).
    #
    # Defensive try/except: VL config errors (bad URL, wrong backend name) must
    # NOT block startup.  /pdf/summarize_section returns 503 until fixed.
    app.state.pdf_vl_service = None
    _vl_render_svc = getattr(app.state, "pdf_render_service", None)
    if _vl_render_svc is not None:
        try:
            from app.services.pdf_vl import PdfVlService  # noqa: PLC0415

            _vl_http_client = getattr(app.state, "openai_http_client", None)
            app.state.pdf_vl_service = PdfVlService(
                pool=pg_pool,
                render_service=_vl_render_svc,
                http_client=_vl_http_client,
            )
            logger.info(
                "PDF VL service ready (§04p Phase 1.D — Qwen-VL via %s)",
                app.state.pdf_vl_service._backend,
            )
        except Exception:
            logger.exception(
                "§04p Phase 1.D VL service init failed — "
                "/pdf/summarize_section will return 503. "
                "Check PDF_VL_BACKEND_URL and PDF_VL_MODEL_ID env vars."
            )
    else:
        logger.warning(
            "§04p Phase 1.D VL service skipped — pdf_render_service is None. "
            "Render service must initialise successfully before the VL service."
        )

    # -------------------------------------------------------------------------
    # 12.5 CC-01 Item 5 — Assessment Report Summarizer
    # -------------------------------------------------------------------------
    # Composes pdf_vl_service. If VL didn't initialise, summarizer stays None
    # and /assessment_summary/* returns 503 until VL recovers.
    app.state.assessment_summarizer = None
    if app.state.pdf_vl_service is not None:
        try:
            from app.services.assessment_summarizer import AssessmentSummarizer  # noqa: PLC0415

            app.state.assessment_summarizer = AssessmentSummarizer(
                pool=pg_pool,
                vl_service=app.state.pdf_vl_service,
            )
            logger.info("AssessmentSummarizer ready (CC-01 Item 5)")
        except Exception:
            logger.exception(
                "CC-01 Item 5 AssessmentSummarizer init failed — "
                "/assessment_summary/* will return 503."
            )

    # -------------------------------------------------------------------------
    # 13. §04p PDF Ingestion Subsystem — Phase 2.A coordinate extraction
    # -------------------------------------------------------------------------
    # PdfCoordinatesService is async-native (asyncpg only — no process pool).
    # Regex over a few KB of text per block is fast enough for the async event
    # loop (typically < 1 ms per block).  No shutdown step required.
    #
    # Depends on silver.pdf_text_blocks being populated first (Phase 1.B).
    # find_coordinates returns empty list + cache_hit=False when no text blocks
    # are cached yet — this is expected before extract_text has run.
    #
    # Depends on silver.pdf_coordinates table being present (Phase 2.A migration).
    # Returns 503 on all /pdf/find_coordinates calls until the table exists and
    # the pool is available.
    app.state.pdf_coordinates_service = None
    try:
        from app.services.pdf_coordinates import PdfCoordinatesService  # noqa: PLC0415

        app.state.pdf_coordinates_service = PdfCoordinatesService(pool=pg_pool)
        logger.info(
            "PDF coordinates service ready (§04p Phase 2.A — deterministic regex)"
        )
    except Exception:
        logger.exception(
            "§04p Phase 2.A coordinates service init failed — "
            "/pdf/find_coordinates will return 503."
        )

    # -------------------------------------------------------------------------
    # Plan §0e — retrieval-trace flush loop. Drains the in-process buffer
    # (populated by agentic_retrieval.persist_node) into silver.query_traces
    # every 5 s or 50 traces. Must start after pg_pool init and before yield.
    # -------------------------------------------------------------------------
    app.state.trace_flush_stop = asyncio.Event()
    app.state.trace_flush_task = None
    try:
        from app.services.trace_writer import run_flush_loop  # noqa: PLC0415

        app.state.trace_flush_task = asyncio.create_task(
            run_flush_loop(pg_pool, stop_event=app.state.trace_flush_stop)
        )
        logger.info("Retrieval trace flush loop started (plan §0e)")
    except Exception:
        logger.exception(
            "Retrieval trace flush loop failed to start — traces will not "
            "be persisted to silver.query_traces."
        )

    # -------------------------------------------------------------------------
    # Application runs here
    # -------------------------------------------------------------------------
    yield

    # -------------------------------------------------------------------------
    # Teardown — close all pools in reverse init order
    # -------------------------------------------------------------------------
    logger.info("Shutting down — closing database pools")

    # Plan §0e — stop the trace flush loop FIRST so the final drain can
    # write any buffered traces while the pg_pool is still open.
    trace_flush_stop = getattr(app.state, "trace_flush_stop", None)
    trace_flush_task = getattr(app.state, "trace_flush_task", None)
    if trace_flush_stop is not None and trace_flush_task is not None:
        try:
            trace_flush_stop.set()
            await asyncio.wait_for(trace_flush_task, timeout=10.0)
            logger.info("Retrieval trace flush loop drained + stopped")
        except asyncio.TimeoutError:
            logger.warning(
                "Retrieval trace flush loop did not drain in 10s — cancelling"
            )
            trace_flush_task.cancel()
        except Exception:
            logger.exception("Trace flush loop shutdown failed (non-fatal)")

    # §04p — shut down the PDF render process pool before DB pools so
    # in-flight render tasks can finish while DB connections are still open.
    pdf_render_service = getattr(app.state, "pdf_render_service", None)
    if pdf_render_service is not None:
        try:
            await pdf_render_service.shutdown()
            logger.info("PDF render service shut down")
        except Exception:
            logger.debug("PDF render service shutdown failed", exc_info=True)

    # §04p Phase 1.B — shut down the extract process pool before DB pools.
    # Must come before pg_pool.close() because the extract service may have
    # in-flight cache writes that need the pool to complete.
    pdf_extract_service = getattr(app.state, "pdf_extract_service", None)
    if pdf_extract_service is not None:
        try:
            await pdf_extract_service.shutdown()
            logger.info("PDF extract service shut down")
        except Exception:
            logger.debug("PDF extract service shutdown failed", exc_info=True)

    # §04p Phase 1.C-i — shut down the layout detection process pool before DB pools.
    # Same rationale as the extract pool shutdown above.
    pdf_layout_service = getattr(app.state, "pdf_layout_service", None)
    if pdf_layout_service is not None:
        try:
            await pdf_layout_service.shutdown()
            logger.info("PDF layout service shut down")
        except Exception:
            logger.debug("PDF layout service shutdown failed", exc_info=True)

    # §04p Phase 1.C-ii — shut down the OCR process pool before DB pools.
    # PaddleOCR workers may have in-flight cache writes; drain them before
    # the pg_pool is closed.
    pdf_ocr_service = getattr(app.state, "pdf_ocr_service", None)
    if pdf_ocr_service is not None:
        try:
            await pdf_ocr_service.shutdown()
            logger.info("PDF OCR service shut down")
        except Exception:
            logger.debug("PDF OCR service shutdown failed", exc_info=True)

    # Anthropic first (no pool, just an httpx client; symmetric teardown order).
    anthropic_client = getattr(app.state, "anthropic_client", None)
    if anthropic_client is not None:
        try:
            await anthropic_client.close()
            logger.info("Anthropic client closed")
        except Exception:
            logger.debug("Anthropic client close failed", exc_info=True)

    # P1 #13 — close the pooled OpenAI-compat httpx client.
    openai_http_client = getattr(app.state, "openai_http_client", None)
    if openai_http_client is not None:
        try:
            await openai_http_client.aclose()
            logger.info("OpenAI-compatible httpx client closed")
        except Exception:
            logger.debug("OpenAI-compatible httpx close failed", exc_info=True)

    await redis_client.aclose()
    logger.info("Redis client closed")

    await neo4j_driver.close()
    logger.info("Neo4j driver closed")

    await qdrant_client.close()
    logger.info("Qdrant client closed")

    await pg_pool.close()
    logger.info("asyncpg pool closed")


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GeoRAG Intelligence",
    description="Geological RAG domain service with cited answers and visualization payloads",
    version="0.1.0",
    lifespan=lifespan,
    # FastAPI review #3 — gate the OpenAPI surface. In dev (default
    # OPENAPI_DOCS_PUBLIC=True) /docs and /redoc work as expected. In
    # prod set OPENAPI_DOCS_PUBLIC=false so the schema + auth-claim
    # shapes don't leak to anyone with network reach.
    docs_url="/docs" if settings.OPENAPI_DOCS_PUBLIC else None,
    redoc_url="/redoc" if settings.OPENAPI_DOCS_PUBLIC else None,
    openapi_url="/openapi.json" if settings.OPENAPI_DOCS_PUBLIC else None,
)

# ──────────────────────────────────────────────────────────────────────────
# Middleware stack — order matters (LIFO, last-added runs first on the
# request path). Order chosen so:
#   1. body-size limit fires FIRST (cheapest reject)
#   2. global timeout wraps everything except SSE streams
#   3. GZip compresses outgoing JSON (skips SSE auto)
#   4. structured access log surrounds all of the above so we record
#      both rejected and accepted requests
# Add LAST so it fires FIRST → list them in REVERSE intended order.
# ──────────────────────────────────────────────────────────────────────────

from fastapi.middleware.gzip import GZipMiddleware  # noqa: E402

from app.middleware import (  # noqa: E402
    BodySizeLimitMiddleware,
    GlobalTimeoutMiddleware,
    StructuredAccessLogMiddleware,
)

# Innermost first (runs LAST).
app.add_middleware(GZipMiddleware, minimum_size=1024)  # FastAPI review #6
app.add_middleware(GlobalTimeoutMiddleware, timeout_s=settings.REQUEST_TIMEOUT_S)  # #2
app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.MAX_REQUEST_BODY_BYTES)  # #1
app.add_middleware(StructuredAccessLogMiddleware)  # #5 — outermost, sees everything

# FastAPI review #9 — rate limiter (gated). slowapi is a Starlette-compat
# limiter; keeping it off by default avoids breaking single-tenant deploys.
if settings.RATE_LIMIT_ENABLED:
    try:
        from slowapi import Limiter  # noqa: PLC0415
        from slowapi.errors import RateLimitExceeded  # noqa: PLC0415
        from slowapi.middleware import SlowAPIMiddleware  # noqa: PLC0415
        from slowapi.util import get_remote_address  # noqa: PLC0415

        async def _rate_limit_handler(request, exc):  # noqa: ARG001
            from starlette.responses import JSONResponse  # noqa: PLC0415
            return JSONResponse(
                {"detail": f"Rate limit exceeded: {exc.detail}"},
                status_code=429,
            )

        app.state.limiter = Limiter(
            key_func=get_remote_address,
            default_limits=[settings.RATE_LIMIT_DEFAULT],
        )
        app.add_middleware(SlowAPIMiddleware)
        app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
        logger.info(
            "Rate limiter enabled — default=%s queries=%s",
            settings.RATE_LIMIT_DEFAULT, settings.RATE_LIMIT_QUERIES,
        )
    except ImportError:
        logger.warning(
            "RATE_LIMIT_ENABLED=true but `slowapi` is not installed. "
            "Add `slowapi` to pyproject.toml dependencies and rebuild "
            "the fastapi image."
        )

# ── Safety layer disable warnings ─────────────────────────────────────────
# FastAPI review #10 (hygiene) — `import logging as _logging` was redundant
# (logging is already imported at module top). Use the existing module logger.
_safety_logger = logging.getLogger("georag.safety")
if not settings.NUMERICAL_VERIFICATION_ENABLED:
    _safety_logger.critical(
        "NUMERICAL_VERIFICATION_ENABLED=False — Layer 3 (numerical claim "
        "verification) is DISABLED. Ungrounded numbers may reach users."
    )
if not settings.ENTITY_RESOLUTION_ENABLED:
    _safety_logger.critical(
        "ENTITY_RESOLUTION_ENABLED=False — Layer 4 (entity resolution) is "
        "DISABLED. Fabricated hole IDs and entity names may reach users."
    )
if not settings.GEOLOGICAL_CONSTRAINTS_ENABLED:
    _safety_logger.critical(
        "GEOLOGICAL_CONSTRAINTS_ENABLED=False — Layer 6 (geological "
        "constraints) is DISABLED. Physically impossible values may reach users."
    )

# Register routers — all /internal/* routes require X-Service-Key auth
# (enforced per-router via the verify_service_key dependency).
app.include_router(queries.router, prefix="/internal")
app.include_router(projects.router, prefix="/internal")
app.include_router(exports_router.router, prefix="/internal")
# Track A.1 Phase 4.B-ii — LLM-assist outlier endpoint called by the
# Dagster outlier detector. /internal/outlier-assist is the path the
# Dagster helper expects (OUTLIER_LLM_ASSIST_ENDPOINT env var defaults
# to http://fastapi:8000/internal/outlier-assist).
app.include_router(outlier_assist_router.router, prefix="/internal")
# Module 6 Phase B Chunk 4a — evidence inspector (no /internal prefix;
# auth is on the router itself via verify_service_key dependency).
app.include_router(evidence_router.router)
# Module 7 Phase B Chunk 1 — answer-run replay + feedback endpoints.
app.include_router(answer_runs_router.router)
# §04p Phase 1.A — PDF Ingestion Subsystem (Stage 2 render endpoints).
# No /internal prefix: these endpoints are called by the Pydantic AI agent
# tools directly, not routed through the Laravel-to-FastAPI internal path.
app.include_router(pdf_router.router)
app.include_router(phase0_ops_router.router)
app.include_router(shadow_trigger_router.router)
app.include_router(mv_refresh_trigger_router.router)  # Phase 2 reliability spec
app.include_router(metrics_ingestion_events_router.router)  # Phase 6 reliability spec
app.include_router(integrations_trigger_router.router)
app.include_router(ocr_render_router.router)
app.include_router(re_ocr_trigger_router.router)
app.include_router(support_agents_router.router)  # Phase G.5 follow-up
app.include_router(visualizations_router.router)  # Phase H4 §5 — strip-log / cross-section / stereonet
app.include_router(trg_cockpit_router.router)     # Phase H4 §8 UI — TRG cockpit + R5 sign-off
app.include_router(report_builder_router.router)  # Phase H4 §7 UI — Report Builder cockpit
app.include_router(ml_training_router.router)     # Phase H4 §12 UI — ML training runs
app.include_router(citation_feedback_router.router)  # Phase H4 §12.8 UI — citation 👍/👎
app.include_router(conflicts_router.router)       # Phase H4 §7.4 UI — Conflict Resolver review queue
app.include_router(audit_findings_router.router)  # Phase H4 §11.5/11.10/6.4 UI — audit findings
app.include_router(what_changed_router.router)    # Phase H4 §9.9 UI — what-changed digest viewer
app.include_router(tier1_misc_router.source_trust_router)
app.include_router(tier1_misc_router.export_gate_router)
app.include_router(tier1_misc_router.k6_router)
app.include_router(tier234_router.rec_router)
app.include_router(tier234_router.qp_router)
app.include_router(tier234_router.ws_members_router)
app.include_router(tier234_router.ws_settings_router)
# tier234_router.ap_router (Kestra channels) removed 2026-05-17.
app.include_router(assessment_summary_router.router)  # CC-01 Item 5 — assessment report structured summary
app.include_router(maps_router.router)  # CC-01 Item 3 (stub) — map ingest scaffold
app.include_router(coverage_router.router)  # CC-03 Item 5 — coverage density heatmap
app.include_router(smdi_router.router)  # SMDI ingestion plan v1.1 Phase 6 — /public-geo/smdi/features
app.include_router(completeness_router.router)  # CC-03 Item 2 — completeness audit
app.include_router(tier234_router.audit_explorer_router)
app.include_router(tier234_router.saved_maps_router)
app.include_router(tier234_router.alerts_router)
app.include_router(tier234_router.phase_h4_health_router)
app.include_router(tier234_router.backups_router)
app.include_router(tier234_router.eval_promotion_router)  # §10.6 promotion gate
app.include_router(tier234_router.eval_questions_router)  # §10-v2 authoring CRUD

# §19.3 Interpretation Workspace — notes / section-lines / target-zones / comments
from app.routers import interpretation as interpretation_router  # noqa: E402
app.include_router(interpretation_router.router)

# §4 Tool Gateway — bind R0/R1 implementations so invoke_tool() can dispatch
from app.services.tool_gateway.impls import register_all_impls  # noqa: E402
register_all_impls()


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe — returns 200 if the process is running."""
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness probe — verifies all database connections are alive.

    Performs a minimal round-trip to each store:
      - asyncpg: SELECT 1
      - Qdrant: collections list
      - Neo4j: RETURN 1
      - Redis: PING

    Returns 503 if any store fails so the container orchestrator can hold
    traffic until the service is genuinely ready.
    """
    # FastAPI review #10 (hygiene) — HTTPException now imported at module
    # top instead of per-call.
    checks: dict[str, str] = {}

    # asyncpg
    try:
        async with app.state.pg_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error: {exc}"

    # Qdrant
    try:
        await app.state.qdrant_client.get_collections()
        checks["qdrant"] = "ok"
    except Exception as exc:
        checks["qdrant"] = f"error: {exc}"

    # Neo4j
    try:
        async with app.state.neo4j_driver.session() as session:
            await session.run("RETURN 1")
        checks["neo4j"] = "ok"
    except Exception as exc:
        checks["neo4j"] = f"error: {exc}"

    # Redis
    try:
        await app.state.redis_client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    all_ok = all(v == "ok" for v in checks.values())
    if not all_ok:
        raise HTTPException(status_code=503, detail={"status": "not ready", "checks": checks})

    return {"status": "ready"}


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint.

    FastAPI review #3 — kept PUBLIC for the current docker-compose
    posture where Prometheus lives on the same internal network and
    port 8000 is not exposed externally. For prod deployments where
    FastAPI sits behind a reverse proxy, gate /metrics there:

      # nginx
      location /metrics {
          satisfy any;
          allow 10.0.0.0/8;        # internal monitoring subnet
          deny  all;
          auth_basic "metrics";
          auth_basic_user_file /etc/nginx/htpasswd-metrics;
          proxy_pass http://fastapi:8000;
      }

    Application-layer auth via X-Service-Key was considered but
    Prometheus 2.x doesn't have a clean `secrets:` env-var path that
    works with docker-compose `.env` substitution — gating here would
    break the existing scrape config for marginal security gain
    (network isolation already covers the threat model).


    Serves the default prometheus_client registry — every Counter/Histogram
    defined in app.metrics is registered there. Prometheus is configured
    to scrape this every 15s (docker/prometheus/prometheus.yml job=fastapi).

    Falls back to an informative 503 text body when the prometheus libs
    aren't installed in the running image — production images should
    install them via the declared pyproject dep, but the fallback keeps
    dev containers that haven't been rebuilt from crashing on this route.
    """
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest, multiprocess  # noqa: PLC0415

        # Import app.metrics so the module's counters/histograms are
        # registered with the default registry before we serialise.
        import app.metrics  # noqa: F401, PLC0415
    except ImportError:
        from starlette.responses import PlainTextResponse  # noqa: PLC0415
        return PlainTextResponse(
            "prometheus_client not installed in this image — rebuild with pip install "
            "prometheus-client prometheus-fastapi-instrumentator",
            status_code=503,
        )

    # Multi-worker uvicorn aggregation: when PROMETHEUS_MULTIPROC_DIR is set
    # each worker writes its counter shards to that directory and the /metrics
    # endpoint sums them via MultiProcessCollector. Without this, only the
    # worker that handled the scrape contributes — so a counter incremented
    # 5× across 5 workers reports as 1.0. See app.metrics module docstring
    # for the matching producer-side dirs.
    import os  # noqa: PLC0415

    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return Response(content=generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
