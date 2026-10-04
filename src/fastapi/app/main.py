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
  app.state.redis_client     — redis.asyncio.Redis
  app.state.anthropic_client — anthropic.AsyncAnthropic | None (B2 — pooled
                               to avoid TLS handshake + pool churn per request;
                               None if LLM_BACKEND != "anthropic" or key unset)
  app.state.embedding_model  — embedding model (EMBEDDING_MODEL_NAME); a shared-
                               sidecar proxy when EMBEDDING_SERVICE_URL is set
                               (default), else a local SentenceTransformer (CPU)
  app.state.reranker         — CrossEncoder (cross-encoder/ms-marco-MiniLM-L-6-v2, CPU)

Timeout constants are imported from app.config.settings so every module
reading them gets the same validated value.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from qdrant_client import AsyncQdrantClient
from starlette.responses import Response  # for /metrics return-type resolution

from app.config import settings
from app.db.dsn import build_dsn, redact_dsn
from app.logging_config import configure_json_logging
from app.routers import admin_tier1_misc as tier1_misc_router  # Phase H4 Tier 1 — source-trust + export-gate + k6
from app.routers import (
    admin_tier234 as tier234_router,  # Phase H4 §11.1/§11.10 backups/cold-tier ops (trimmed 2026-07-28, task #31)
)
from app.routers import answer_runs as answer_runs_router
from app.routers import audit_findings as audit_findings_router  # Phase H4 §11.5/11.10/6.4 UI
from app.routers import citation_feedback as citation_feedback_router  # Phase H4 §12.8 UI
from app.routers import coverage as coverage_router  # CC-03 Item 5 — coverage density heatmap
from app.routers import evidence as evidence_router
from app.routers import exports as exports_router
from app.routers import integrations_trigger as integrations_trigger_router
from app.routers import maps as maps_router  # CC-01 Item 3 (stub) — map ingest scaffold
from app.routers import metrics_ingestion_events as metrics_ingestion_events_router
from app.routers import ml_training as ml_training_router  # Phase H4 §12 UI
from app.routers import mv_refresh_trigger as mv_refresh_trigger_router
from app.routers import outlier_assist as outlier_assist_router
from app.routers import phase0_ops as phase0_ops_router
from app.routers import projects, queries
from app.routers import public_geo_trigger as public_geo_trigger_router
from app.routers import shadow_trigger as shadow_trigger_router
from app.routers import smdi as smdi_router  # SMDI ingestion plan v1.1 Phase 6 — features endpoint
from app.routers import visualizations as visualizations_router  # Phase H4 §5
from app.routers import what_changed as what_changed_router  # Phase H4 §9.9 UI
from app.routers import workflow_trigger as workflow_trigger_router  # HAT-13
from app.services.qdrant_conn import qdrant_client_kwargs

# V1.5-05 — switch to JSON logs at module import so every logger.info() in
# the app emits a structured payload Promtail can ingest with a single
# `| json` pipeline stage. Pairs with V1.5-04 on the Laravel side.
configure_json_logging(level=settings.LOG_LEVEL.upper())

logger = logging.getLogger(__name__)


def _statement_cache_size_from_env() -> int:
    """asyncpg ``statement_cache_size`` from ``ASYNCPG_STATEMENT_CACHE_SIZE``.

    0 (the default, and the value for anything unset, blank, negative or not
    an integer) is the PgBouncer-transaction-mode-safe setting. A positive
    value is only valid where asyncpg talks to Postgres directly (the AWS
    deployment has no pooler, so it can set 100).
    """
    raw = (os.environ.get("ASYNCPG_STATEMENT_CACHE_SIZE") or "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "ASYNCPG_STATEMENT_CACHE_SIZE=%r is not an integer — using 0", raw,
        )
        return 0
    if value < 0:
        logger.warning(
            "ASYNCPG_STATEMENT_CACHE_SIZE=%d is negative — using 0", value,
        )
        return 0
    return value


def qdrant_dense_dim(vectors_config: Any) -> int | None:
    """Read the canonical dense vector size out of a Qdrant vectors config.

    Qdrant's ``collection.config.params.vectors`` has two shapes: a bare
    ``VectorParams`` when the collection has a single unnamed dense vector,
    and a ``dict`` keyed by slot name once named vectors exist. georag_chunks
    is the dict form (dense in the unnamed ``""`` slot alongside the named
    sparse slot), but both are handled so this stays correct if the schema
    is ever simplified.

    Returns ``None`` when the dense size can't be determined — callers treat
    that as "unknown, don't act" rather than as a mismatch.
    """
    if isinstance(vectors_config, dict):
        dense = vectors_config.get("")
        return getattr(dense, "size", None) if dense is not None else None
    return getattr(vectors_config, "size", None)


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
      5. Embedding model — shared sidecar proxy (EMBEDDING_SERVICE_URL) or local
      6. CrossEncoder reranker (cross-encoder/ms-marco-MiniLM-L-6-v2, CPU)

    Teardown is the mirror: each client is closed in reverse order so
    in-flight requests can complete before their pools disappear.
    """
    # -------------------------------------------------------------------------
    # 0. Logfire — instrument BEFORE other resources so spans
    #    wrap pool creation, the embedding-model warmup, and the first request.
    #    See settings.LOGFIRE_* and the SECURITY.md "Observability" section
    #    for the rollout recipe.
    # -------------------------------------------------------------------------
    if settings.LOGFIRE_ENABLED:
        try:
            import logfire  # noqa: PLC0415

            # Send to the hosted backend when configured; otherwise keep
            # spans in-process for local debugging.
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
            else:
                logfire.configure(
                    service_name=settings.LOGFIRE_SERVICE_NAME,
                    environment=settings.LOGFIRE_ENVIRONMENT,
                    send_to_logfire=False,
                )
                logger.warning(
                    "Logfire configured in LOCAL-ONLY mode (LOGFIRE_TOKEN unset)"
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
    # Pooled (PgBouncer) DSN — the request path wants short transactions
    # and high connection churn, which is what a transaction pooler is for.
    # Background work uses build_dsn() (direct) instead; see app/db/dsn.py.
    pg_dsn = build_dsn(direct=False, scheme="postgresql", include_sslmode=True)
    # Log the DSN that was actually built, redacted -- not settings fields
    # read a second time. The old form could report a different host from
    # the one it had just connected to, which is precisely the failure
    # mode you are reading this log line to diagnose.
    # CodeQL flags this (py/clear-text-logging-sensitive-data, high)
    # because a DSN built from POSTGRES_PASSWORD reaches a log call, and
    # its taint tracking cannot see that redact_dsn strips the password
    # component structurally (app/db/dsn.py, asserted in
    # tests/test_build_dsn_single_source.py::TestRedactDsn, including the
    # malformed-port case that used to raise instead of redacting).
    #
    # A `# codeql[...]` comment does NOT suppress this -- GitHub code
    # scanning does not honour inline suppression comments, and one sat
    # on this line raising the alert anyway. The alert is dismissed
    # through the API instead, with this reasoning as the dismissal
    # comment. Do not re-add a suppression marker; it only looks like a
    # guard.
    #
    # The real guard is TestRedactDsn: if redact_dsn ever stops
    # redacting, those tests fail before this line can leak anything.
    #
    # Not fixed by logging components instead of the built DSN -- see the
    # paragraph above about reporting a host you did not connect to.
    logger.info("Connecting asyncpg pool -> %s", redact_dsn(pg_dsn))
    _pg_pool_min, _pg_pool_max = 2, 12
    # Audit item 30: the prepared-statement cache is only unsafe BEHIND
    # PgBouncer in transaction mode (compose). It is a real cost everywhere
    # else (every query re-parsed), so it is env-driven. Default 0 = the
    # PgBouncer-safe value, which is what compose and any unset environment
    # get. The AWS deployment has NO pooler and may set
    # ASYNCPG_STATEMENT_CACHE_SIZE=100. Never set it above 0 behind
    # transaction-mode PgBouncer: it fails under load with
    # `prepared statement "__asyncpg_stmt_N__" does not exist`.
    _stmt_cache_size = _statement_cache_size_from_env()
    pg_pool: asyncpg.Pool = await asyncpg.create_pool(
        dsn=pg_dsn,
        min_size=_pg_pool_min,
        # FastAPI review #4 — per-worker max trimmed from 25 → 12. The
        # ceiling is PER UVICORN WORKER, so per task it is workers × 12:
        #   * docker/fastapi.Dockerfile defaults UVICORN_WORKERS=6 → 72
        #     (this comment used to assume 4 workers → 48);
        #   * compose runs UVICORN_WORKERS=3 → 36, behind PgBouncer's
        #     default_pool_size=100.
        # AWS has NO pooler: every fastapi task holds up to 72 direct RDS
        # connections, plus the hatchet worker's and Laravel's, against a
        # max_connections that scales with instance memory (~225 on
        # db.t4g.small). Re-derive before scaling tasks or workers up.
        max_size=_pg_pool_max,
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
        statement_cache_size=_stmt_cache_size,
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
        "asyncpg pool ready (min=%d max=%d per worker, statement_cache_size=%d, "
        "jit=off)",
        _pg_pool_min,
        _pg_pool_max,
        _stmt_cache_size,
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
    # qdrant_client_kwargs() is the single source of truth for host/port/
    # https/api_key (11 other call sites already use it) — this was the one
    # remaining construction site still building the dict inline, agreeing
    # with the helper only by coincidence of matching settings values.
    qdrant_client = AsyncQdrantClient(
        **qdrant_client_kwargs(),
        timeout=int(settings.TIMEOUT_QDRANT_S),
        check_compatibility=False,  # avoids a blocking HTTP call at startup
    )
    app.state.qdrant_client = qdrant_client
    logger.info(
        "Qdrant client ready (api_key_set=%s)",
        bool(settings.QDRANT_API_KEY),
    )

    # -------------------------------------------------------------------------
    # 3. Neo4j — REMOVED 2026-07-28 (B1). app.state.neo4j_driver is gone;
    # AgentDeps.neo4j_driver is passed as None from routers/queries.py.
    # Every consumer (traverse_knowledge_graph, query_graph_by_label,
    # fetch_project_graph_entities, the Layer 4 entity-resolution
    # validators, the Phase 0 graph-health agents) already failed open on a
    # missing/unreachable driver, so this formalizes an already-common
    # runtime path rather than introducing a new failure mode.
    # -------------------------------------------------------------------------

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
    # 5. Query-time embedding model
    # -------------------------------------------------------------------------
    # get_embedding_model() branches on EMBEDDING_BACKEND: "cohere" (the
    # default since ADR-0025) returns a lightweight Cohere Embed 5 client on
    # Cohere's own API (no local model, no download); "bedrock" is the Embed v4
    # rollback (config.py's Qwen/Qwen3-Embedding-0.6B, 1024-dim, is the
    # self-hosted fallback for dev and on-prem, EMBEDDING_BACKEND=local). Batch
    # document indexing runs through the Hatchet ingest_pdf workflow's
    # passage_embedder, which reads the same EMBEDDING_BACKEND flag — Dagster
    # dropped from this deployment entirely in Phase B2.
    logger.info("Loading embedding model: %s", settings.EMBEDDING_MODEL_NAME)
    _t0 = time.perf_counter()
    # Shared embedding sidecar when EMBEDDING_SERVICE_URL is set (one model
    # for all workers over a localhost hop); else a local CPU model as
    # before. See app.services.embedding.
    from app.services.embedding import (  # noqa: PLC0415
        EMBEDDING_DISABLED,
        EmbeddingReadiness,
        get_embedding_model,
        rewarm_until_ready,
        warm_up_once,
    )

    # VEN-1 (2026-09-29): a failed warm-up no longer disables the embedder
    # for the life of the process. Only a failure to BUILD the model, or a
    # confirmed dimension mismatch, takes it out of service; a failed warm-up
    # encode keeps it (queries still try it), retries in the background with
    # backoff, and /ready reports "warming" until it succeeds.
    embedding_readiness = EmbeddingReadiness()
    app.state.embedding_readiness = embedding_readiness
    app.state.embedding_rewarm_task = None
    embedding_model: Any = None
    try:
        embedding_model = get_embedding_model(
            settings.EMBEDDING_MODEL_NAME,
            settings.EMBEDDING_MODEL_REVISION,
        )
    except Exception as exc:
        logger.exception(
            "Failed to load embedding model — search_documents will return empty results"
        )
        embedding_readiness.state = EMBEDDING_DISABLED
        embedding_readiness.detail = f"load failed: {type(exc).__name__}"
    app.state.embedding_model = embedding_model

    def _disable_embedding(reason: str) -> None:
        # Audit 2026-06-27 (C1): the fail-fast dimension-parity check. A
        # model whose dim disagrees with EMBEDDING_DIMENSION (and thus the
        # live Qdrant collection) would silently break retrieval, so it is
        # disabled and search_documents returns empty (safe refusal)
        # instead of querying a mismatched vector space.
        logger.critical(
            "Embedding dim mismatch (%s, model %s). Disabling embedding model "
            "to avoid querying a mismatched Qdrant collection. Fix "
            "EMBEDDING_MODEL_NAME/EMBEDDING_DIMENSION or re-embed the corpus.",
            reason,
            settings.EMBEDDING_MODEL_NAME,
        )
        app.state.embedding_model = None
        embedding_readiness.state = EMBEDDING_DISABLED
        embedding_readiness.detail = f"dimension mismatch: {reason}"

    if embedding_model is not None:
        # Warm up: encode a dummy string so the first real request does not
        # pay the JIT/model-init penalty (a round-trip for the sidecar proxy
        # and Bedrock, which also validates connectivity at startup).
        _warm = warm_up_once(embedding_model, embedding_readiness)
        _elapsed = time.perf_counter() - _t0
        try:
            _loaded_dim = embedding_model.get_sentence_embedding_dimension()
        except Exception:  # noqa: BLE001 — logged below as unknown
            logger.debug("embedding dimension unavailable after warm-up", exc_info=True)
            _loaded_dim = None
        # %s, not %d — the remote-sidecar proxy returns None for the dimension
        # when the sidecar can't be reached, and %d would blow up the log call.
        logger.info(
            "Embedding model %s: %s (dim=%s) in %.2fs",
            "ready" if _warm else "loaded but NOT warm",
            settings.EMBEDDING_MODEL_NAME,
            _loaded_dim,
            _elapsed,
        )
        _mismatch = (
            f"model reports dim={_loaded_dim} but EMBEDDING_DIMENSION={settings.EMBEDDING_DIMENSION}"
            if _loaded_dim is not None and int(_loaded_dim) != settings.EMBEDDING_DIMENSION
            else None
        )
        if _mismatch is not None:
            _disable_embedding(_mismatch)
        elif not _warm:
            app.state.embedding_rewarm_task = asyncio.create_task(
                rewarm_until_ready(
                    embedding_model,
                    embedding_readiness,
                    expected_dim=settings.EMBEDDING_DIMENSION,
                    on_disable=_disable_embedding,
                )
            )

    # -------------------------------------------------------------------------
    # 5b. Qdrant collection dimension parity
    # -------------------------------------------------------------------------
    # The check above compares the LOADED MODEL to EMBEDDING_DIMENSION. Both can
    # agree with each other and still disagree with the collection we actually
    # query — e.g. config says 1024/Qwen3 but georag_chunks was created at 384
    # by an older bge-era run, or a re-embed was never executed. Symptom is an
    # opaque HTTP 400 from Qdrant on every search, which surfaces to the user as
    # a bare refusal with no cause. Dagster's index_document_passages already
    # guards its write path this way (audit 2026-06-27 C1); the read path did
    # not, so this closes the other half.
    #
    # Deliberately fail-soft: a Qdrant that is slow or still starting must not
    # block FastAPI startup, and a missing collection is the normal state on a
    # fresh install. Only a confirmed dimension DISAGREEMENT disables the model,
    # which degrades search to a safe refusal rather than querying a mismatched
    # vector space.
    if app.state.embedding_model is not None:
        from app.services.ingest.passage_embedder import (  # noqa: PLC0415
            _QDRANT_COLLECTION as _CHUNKS_COLLECTION,
        )

        try:
            _info = await qdrant_client.get_collection(_CHUNKS_COLLECTION)
            _collection_dim = qdrant_dense_dim(_info.config.params.vectors)
            if (
                _collection_dim is not None
                and _collection_dim != settings.EMBEDDING_DIMENSION
            ):
                logger.critical(
                    "Qdrant collection dim mismatch: '%s' dense dim=%d but "
                    "EMBEDDING_DIMENSION=%d (model %s). Disabling embedding "
                    "model — every search would 400 against this collection. "
                    "Re-embed the corpus (scripts/reembed_qdrant.py) or point "
                    "EMBEDDING_MODEL_NAME/EMBEDDING_DIMENSION at the model the "
                    "collection was built with.",
                    _CHUNKS_COLLECTION,
                    _collection_dim,
                    settings.EMBEDDING_DIMENSION,
                    settings.EMBEDDING_MODEL_NAME,
                )
                app.state.embedding_model = None
                embedding_readiness.state = EMBEDDING_DISABLED
                embedding_readiness.detail = (
                    f"Qdrant '{_CHUNKS_COLLECTION}' dim={_collection_dim} != "
                    f"EMBEDDING_DIMENSION={settings.EMBEDDING_DIMENSION}"
                )
                _rewarm = app.state.embedding_rewarm_task
                if _rewarm is not None:
                    _rewarm.cancel()
            else:
                logger.info(
                    "Qdrant collection '%s' dense dim=%s matches "
                    "EMBEDDING_DIMENSION=%d",
                    _CHUNKS_COLLECTION,
                    _collection_dim,
                    settings.EMBEDDING_DIMENSION,
                )
        except Exception as exc:  # noqa: BLE001 — never block startup on Qdrant
            logger.warning(
                "Could not verify Qdrant collection '%s' dimension at startup "
                "(%s: %s). Skipping the parity check — a mismatch will surface "
                "as an HTTP 400 on the first search instead.",
                _CHUNKS_COLLECTION,
                type(exc).__name__,
                exc,
            )

    # -------------------------------------------------------------------------
    # 6. Cross-encoder reranker — BAAI/bge-reranker-base (Module 4 Chunk 3)
    # -------------------------------------------------------------------------
    # bge-reranker-base (Apache 2.0, ~278 MB) replaces ms-marco-MiniLM-L-6-v2.
    # It is pinned by HuggingFace revision SHA (see reranker.py) so weight
    # drift is detected via the version string in answer_runs.reranker_version.
    #
    # CORRECTED 2026-08-22. This used to claim: "The reranker now runs on
    # the FUSED candidate set (post cross-store RRF), not just on
    # Qdrant-only results. Per-class top-k is defined in
    # app.services.reranker.RERANKER_TOP_K_BY_CLASS."
    #
    # Neither half is true. Reranking runs INSIDE `search_documents`, over
    # Qdrant candidates only — nothing ever fuses Qdrant results with the
    # PostGIS/assay tool results, so there is no cross-store RRF pool for
    # it to run on. And `top_k_for_class` has zero callers anywhere in the
    # tree, so RERANKER_TOP_K_BY_CLASS is inert; the single
    # RERANKER_TOP_K value is what applies.
    #
    # Fallback policy (spec B6): if the reranker fails to load or predict,
    # log + continue with RRF order. Do not fail the query.
    _t1 = time.perf_counter()
    try:
        from app.services.reranker import (  # noqa: PLC0415
            RERANKER_BACKEND,
            active_reranker_version,
            get_reranker_or_none,
        )

        # get_reranker_or_none() implements the full backend precedence:
        # RERANKER_BACKEND=foundry (Cohere Rerank v4, no local model at all)
        # > RERANKER_SERVICE_URL (shared sidecar HTTP proxy, avoids the
        # per-worker OOM from 6 uvicorn workers each loading a ~1 GiB model)
        # > in-process CrossEncoder singleton. Delegating here instead of
        # duplicating the sidecar-vs-local branch keeps this single source
        # of truth in services/reranker.py.
        reranker = get_reranker_or_none()
        _elapsed_r = time.perf_counter() - _t1
        _version = active_reranker_version()
        app.state.reranker = reranker
        app.state.reranker_version = _version if reranker is not None else None
        if reranker is None:
            logger.warning(
                "Reranker unavailable (backend=%s) — rerank step will be "
                "skipped (RRF order used)",
                RERANKER_BACKEND,
            )
        else:
            logger.info(
                "Reranker ready: backend=%s version=%s loaded in %.2fs",
                RERANKER_BACKEND, _version, _elapsed_r,
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
    # S3BronzeStore (SeaweedFS) is the production Bronze store as of the
    # storage-abstraction plan's PR4; LocalFsBronzeStore is a fallback for a
    # bare dev shell that isn't running the full docker-compose stack.
    try:
        import os as _os  # noqa: PLC0415

        from app.services.bronze_store import LocalFsBronzeStore, S3BronzeStore  # noqa: PLC0415
        from app.services.pdf_render import PdfRenderService  # noqa: PLC0415

        app.state.pdf_render_service = PdfRenderService()
        try:
            app.state.bronze_store = S3BronzeStore()
            logger.info(
                "PDF render service ready; Bronze store backed by object storage (STORAGE_BACKEND=%s)",
                _os.environ.get("STORAGE_BACKEND", "s3_compatible"),
            )
        except ValueError:
            # No object-storage credentials in this environment (AWS_ACCESS_KEY_ID/
            # AWS_SECRET_ACCESS_KEY, or a legacy S3_*/MINIO_*/SEAWEEDFS_* fallback,
            # are all unset) — expected in a bare dev shell, not in any environment
            # actually running SeaweedFS. Fall back to local disk rather than
            # failing PDF render entirely.
            logger.warning(
                "No object-storage credentials found — Bronze store falling back to "
                "local disk. Fine for a bare dev shell; any environment with more than "
                "one FastAPI instance needs SeaweedFS-backed storage to work correctly."
            )
            app.state.bronze_store = LocalFsBronzeStore()
            logger.info("PDF render service and Bronze store ready (local-disk fallback)")
    except Exception:
        logger.exception(
            "§04p PDF subsystem init failed — /pdf/* endpoints will return 503. "
            "Ensure pikepdf and pypdfium2 are installed: uv pip install 'pikepdf>=9.0' 'pypdfium2>=4.30'"
        )
        app.state.pdf_render_service = None
        app.state.bronze_store = None

    # -------------------------------------------------------------------------
    # 9. §04p Stage 3 extract service — NOT started since 2026-09-29.
    # -------------------------------------------------------------------------
    # Its only consumers were GET /pdf/extract_text and /pdf/find_tables,
    # unmounted by database audit PG-13 (silver.pdf_text_blocks /
    # pdf_table_cells are never created). Starting it only spawned an unused
    # process pool in every worker. app.state.pdf_extract_service stays None.
    app.state.pdf_extract_service = None

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
    #   PDF_VL_BACKEND_URL — full base URL (no default; the local vLLM
    #                        service was removed 2026-07-30)
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
    # 12.5 / 13. AssessmentSummarizer and PdfCoordinatesService — NOT started
    # since 2026-09-29. Their only consumers (/assessment_summary/*,
    # /completeness_audit/*, GET /pdf/find_coordinates) were unmounted by
    # database audit PG-13: they read silver.pdf_text_blocks /
    # silver.pdf_coordinates, which no migration creates.
    # -------------------------------------------------------------------------
    app.state.assessment_summarizer = None
    app.state.pdf_coordinates_service = None

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

    # VEN-1 — stop a still-running embedding re-warm loop.
    _rewarm_task = getattr(app.state, "embedding_rewarm_task", None)
    if _rewarm_task is not None and not _rewarm_task.done():
        _rewarm_task.cancel()

    # Audit item 17: child rows and usage metering are written by retained
    # background tasks (agentic_retrieval.persist_node). Let them finish while
    # the pg_pool is still open.
    try:
        from app.agent.agentic_retrieval.nodes import (  # noqa: PLC0415
            drain_persist_background,
        )

        await drain_persist_background(timeout=10.0)
    except Exception:
        logger.exception("Persist background drain failed (non-fatal)")

    # Plan §0e — stop the trace flush loop FIRST so the final drain can
    # write any buffered traces while the pg_pool is still open.
    trace_flush_stop = getattr(app.state, "trace_flush_stop", None)
    trace_flush_task = getattr(app.state, "trace_flush_task", None)
    if trace_flush_stop is not None and trace_flush_task is not None:
        try:
            trace_flush_stop.set()
            await asyncio.wait_for(trace_flush_task, timeout=10.0)
            logger.info("Retrieval trace flush loop drained + stopped")
        except TimeoutError:
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

    await qdrant_client.close()
    logger.info("Qdrant client closed")

    await pg_pool.close()
    logger.info("asyncpg pool closed")

    # API-12 — the ingest-progress module pool is created lazily from the
    # request path (shadow / mv-refresh triggers) and was never closed.
    try:
        from app.hatchet_workflows._progress import (  # noqa: PLC0415
            close_pool as _close_progress_pool,
        )

        await _close_progress_pool()
    except Exception:
        logger.warning("ingest-progress pool close failed", exc_info=True)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="GeoRAG Intelligence",
    description="Geological RAG domain service with cited answers and visualization payloads",
    version="0.1.0",
    lifespan=lifespan,
    # FastAPI review #3 — gate the OpenAPI surface. Off by default
    # (OPENAPI_DOCS_PUBLIC=False since API-11) so the schema + auth-claim
    # shapes don't leak to anyone with network reach; a developer sets
    # OPENAPI_DOCS_PUBLIC=true locally to get /docs and /redoc.
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
        from slowapi.errors import RateLimitExceeded  # noqa: PLC0415

        from app.services.rate_limit import (  # noqa: PLC0415
            ProbeExemptSlowAPIMiddleware,
        )
        from app.services.rate_limit import (  # noqa: PLC0415
            limiter as _actor_limiter,
        )

        async def _rate_limit_handler(request, exc):  # noqa: ARG001
            from starlette.responses import JSONResponse  # noqa: PLC0415
            return JSONResponse(
                {"detail": f"Rate limit exceeded: {exc.detail}"},
                status_code=429,
            )

        # API-6 — ONE limiter, keyed per (workspace, user) from the JWT
        # (then X-Workspace-Id, then IP). This used to be a second Limiter
        # keyed on get_remote_address; every request arrives from a few
        # Laravel task IPs, so all tenants shared one 60/min bucket, and the
        # per-actor limiter's route registrations were invisible to it.
        app.state.limiter = _actor_limiter
        app.add_middleware(ProbeExemptSlowAPIMiddleware)
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

# API-5 — every posture CRITICAL below starts with this literal token so ONE
# CloudWatch metric filter (deploy/aws/terraform/alerts.tf, log_markers)
# matches all of them. The Azure-era "georag-fastapi-critical" alert that
# the comments here used to rely on does not exist on AWS; nothing matched
# a bare `"level": "CRITICAL"` line. Keep it a literal (not built at
# runtime) — scripts/check-log-marker-alarms.py greps for it.
POSTURE_CRITICAL_MARKER = "GEORAG_POSTURE_CRITICAL"

if not settings.NUMERICAL_VERIFICATION_ENABLED:
    _safety_logger.critical(
        "%s NUMERICAL_VERIFICATION_ENABLED=False — Layer 3 (numerical claim "
        "verification) is DISABLED. Ungrounded numbers may reach users.",
        POSTURE_CRITICAL_MARKER,
    )
if not settings.ENTITY_RESOLUTION_ENABLED:
    _safety_logger.critical(
        "%s ENTITY_RESOLUTION_ENABLED=False — Layer 4 (entity resolution) is "
        "DISABLED. Fabricated hole IDs and entity names may reach users.",
        POSTURE_CRITICAL_MARKER,
    )
if not settings.GEOLOGICAL_CONSTRAINTS_ENABLED:
    _safety_logger.critical(
        "%s GEOLOGICAL_CONSTRAINTS_ENABLED=False — Layer 6 (geological "
        "constraints) is DISABLED. Physically impossible values may reach users.",
        POSTURE_CRITICAL_MARKER,
    )


def _assert_production_posture() -> None:
    """Say out loud when production is running with a control switched off.

    The controls above default to True, so their warnings fire only when
    someone deliberately turns one off. This block is for the opposite and
    more dangerous shape: settings that default to FALSE for a developer's
    convenience and were never turned on in production.

    .env.production.example has prescribed PROMPT_INJECTION_DELIMITING_ENABLED
    and RATE_LIMIT_ENABLED under the comment "Must be ON in production" for a
    long time. Neither was ever set on fastapi-cc or hatchet-worker-cc, both
    default to False, and both gate real code — the slowapi limiter install
    and the fence that marks retrieved third-party text as untrusted. So a
    PDF containing "ignore previous instructions" went into the prompt
    unfenced, and nothing throttled anything, for as long as the deployment
    has existed. Nothing said so, because nothing knew it was production.

    A CRITICAL line rather than a refusal to start: these are hardening
    controls, and taking the API down over one would trade a quiet risk for a
    loud outage. It is not a whisper either: every line carries the
    ``GEORAG_POSTURE_CRITICAL`` token (``POSTURE_CRITICAL_MARKER``), which
    the log-marker metric filter in deploy/aws/terraform/alerts.tf pages on.
    (It used to say "pages via georag-fastapi-critical" — an Azure alert that
    did not survive the move to AWS, so these lines paged nobody.)
    """
    if not settings.is_production:
        return

    required_on = (
        (
            "PROMPT_INJECTION_DELIMITING_ENABLED",
            settings.PROMPT_INJECTION_DELIMITING_ENABLED,
            "retrieved third-party text is concatenated into the prompt with "
            "no fence marking it as untrusted data",
        ),
        (
            "RATE_LIMIT_ENABLED",
            settings.RATE_LIMIT_ENABLED,
            "no request throttling of any kind is installed",
        ),
    )

    for name, value, consequence in required_on:
        if not value:
            _safety_logger.critical(
                "%s GEORAG_ENV=production but %s is off — %s. "
                "Set it on the task definition.",
                POSTURE_CRITICAL_MARKER, name, consequence,
            )

    # API-11 — the table OWNER is exempt from plain ENABLE ROW LEVEL
    # SECURITY, so connecting as it silently disables tenant isolation on
    # every table that is not FORCE'd. Every deploy target sets
    # POSTGRES_USER=georag_app today; this catches the day one does not.
    if settings.POSTGRES_USER.strip() == "georag":
        _safety_logger.critical(
            "%s GEORAG_ENV=production with POSTGRES_USER=georag, the table "
            "owner — row-level security does not apply to the owner on "
            "tables without FORCE ROW LEVEL SECURITY. Set POSTGRES_USER="
            "georag_app on the task definition.",
            POSTURE_CRITICAL_MARKER,
        )

    if settings.QDRANT_DOCUMENT_PROJECT_SCOPE == "cross_project":
        _safety_logger.critical(
            "%s GEORAG_ENV=production with QDRANT_DOCUMENT_PROJECT_SCOPE="
            "cross_project — document retrieval filters on workspace only, "
            "so a question asked in one project can be answered from another "
            "project's reports.",
            POSTURE_CRITICAL_MARKER,
        )

    # ADR-0023 — the selected chat backend must carry the credential it
    # needs. Unlike the controls above this is not a hardening switch: with
    # it missing, every query fails at the first call. It is still a CRITICAL
    # rather than a refusal to start, because this container also serves
    # ingest, render and health, and taking all of that down over a chat
    # credential trades one broken capability for four.
    #
    # ECS normally makes this unreachable — a task referencing a Secrets
    # Manager key that does not exist never starts at all. What it does NOT
    # catch is the key existing with an empty or whitespace value, which is
    # exactly the shape a half-finished `put-secret-value` leaves behind.
    _chat_credentials = {
        "cohere": ("COHERE_API_KEY", settings.COHERE_API_KEY),
        "bedrock": ("BEDROCK_CHAT_MODEL_ID", settings.BEDROCK_CHAT_MODEL_ID),
        "anthropic": ("ANTHROPIC_API_KEY", settings.ANTHROPIC_API_KEY),
        "vllm": ("VLLM_URL", settings.VLLM_URL),
    }
    _required = _chat_credentials.get(settings.LLM_BACKEND)
    if _required and not _required[1].strip():
        _safety_logger.critical(
            "%s GEORAG_ENV=production with LLM_BACKEND=%s but %s is empty — "
            "every chat query will fail at the first call. Set it on the "
            "task definition (it is written to Secrets Manager out of band; "
            "see deploy/aws/README.md).",
            POSTURE_CRITICAL_MARKER, settings.LLM_BACKEND, _required[0],
        )


_assert_production_posture()

# Register routers — all /internal/* routes require X-Service-Key auth
# (enforced per-router via the verify_service_key dependency).
app.include_router(queries.router, prefix="/internal")
app.include_router(projects.router, prefix="/internal")
app.include_router(exports_router.router)
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
# API-14 — /pdf/* (6 routes) and /assessment_summary/* (2) were removed
# 2026-09-29: no caller anywhere, and nothing writes the Bronze
# ``pdfs/{sha256}.pdf`` layout both read, so every call 404'd. The lifespan
# services they used (render/extract pools, VL, assessment summarizer) are
# still initialised above; removing those is a separate lifespan change.
app.include_router(phase0_ops_router.router)
app.include_router(shadow_trigger_router.router)
app.include_router(public_geo_trigger_router.router)  # operator "Sync now" for public_geo_sync
app.include_router(workflow_trigger_router.router)  # HAT-13 triggers for UI-only workflows
app.include_router(mv_refresh_trigger_router.router)  # Phase 2 reliability spec
app.include_router(metrics_ingestion_events_router.router)  # Phase 6 reliability spec
app.include_router(integrations_trigger_router.router)
app.include_router(visualizations_router.router)  # Phase H4 §5 — strip-log / cross-section / stereonet
app.include_router(ml_training_router.router)     # Phase H4 §12 UI — ML training runs
app.include_router(citation_feedback_router.router)  # Phase H4 §12.8 UI — citation 👍/👎
app.include_router(audit_findings_router.router)  # Phase H4 §11.5/11.10/6.4 UI — audit findings
app.include_router(what_changed_router.router)    # Phase H4 §9.9 UI — what-changed digest viewer
app.include_router(tier1_misc_router.source_trust_router)
app.include_router(tier1_misc_router.export_gate_router)
app.include_router(tier1_misc_router.k6_router)
# tier234_router.{rec,qp,ws_members,ws_settings,audit_explorer,saved_maps,
# alerts,phase_h4_health}_router — REMOVED 2026-07-28 (task #31). Zero
# Laravel-side callers for any of the 8; the admin pages that reached them
# were deleted in the reader-core trim. See admin_tier234.py's module
# docstring. tier234_router.ap_router (Kestra channels) was already removed
# 2026-05-17.
# completeness_router — UNMOUNTED 2026-09-29 (database audit PG-13). It reads
# silver.pdf_text_blocks / silver.pdf_coordinates, which no migration creates,
# and had no caller. The module stays. /assessment_summary/* was removed
# outright (API-14, above).
app.include_router(maps_router.router)  # CC-01 Item 3 (stub) — map ingest scaffold
app.include_router(coverage_router.router)  # CC-03 Item 5 — coverage density heatmap
app.include_router(smdi_router.router)  # SMDI ingestion plan v1.1 Phase 6 — /public-geo/smdi/features
app.include_router(tier234_router.backups_router)  # Phase H4 §11.1/§11.10 — backup / cold-tier ops

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
    """Readiness probe — verifies stores are alive and the embedder is warm.

    Performs a minimal round-trip to each store:
      - asyncpg: SELECT 1
      - Qdrant: collections list
      - Redis: PING
    and reports the query-path embedder's warm-up state (VEN-1) without
    calling it -- a probe must not spend a Bedrock call every few seconds.

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

    # Neo4j — REMOVED 2026-07-28 (B1). Was checked here; an Azure readiness
    # probe reading this endpoint would otherwise mark the pod perpetually
    # unhealthy for a store that no longer exists.

    # Redis
    try:
        await app.state.redis_client.ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {exc}"

    # Query-path embedder (VEN-1, 2026-09-29). A task whose warm-up failed
    # reports "warming" until the background re-warm succeeds, and one whose
    # model was disabled (load failure, dimension mismatch) reports
    # "disabled" -- either way it cannot answer a document question, and
    # this endpoint used to say it was ready regardless.
    #
    # NOTE: production ECS container health checks call /health, not
    # /ready (deploy/aws/terraform/services.tf), so this does NOT make ECS
    # replace the task -- the background re-warm is the recovery. Pointing
    # the ECS check at /ready is a separate operator decision: it would also
    # recycle tasks on a transient Postgres/Redis blip.
    readiness = getattr(app.state, "embedding_readiness", None)
    if readiness is not None:
        checks["embedding"] = readiness.describe()

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
        from prometheus_client import (  # noqa: PLC0415
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            generate_latest,
            multiprocess,
        )

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
