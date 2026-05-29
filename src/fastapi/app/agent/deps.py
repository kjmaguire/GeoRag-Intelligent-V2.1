"""Agent dependency injection container for the GeoRAG Pydantic AI agent.

AgentDeps is passed as the ``deps`` argument to every ``geo_agent.run()`` /
``geo_agent.run_stream()`` call.  Inside a tool the container is accessed via
``ctx.deps``, giving every tool typed, async-safe access to every database
pool without going through global state.

Design notes
------------
- ``dataclass`` (not Pydantic BaseModel) because Pydantic AI's RunContext
  expects a plain Python object for deps — it does not validate the deps
  container itself.
- All three clients are intentionally non-optional.  If a pool fails to
  initialise at startup the lifespan hook raises before any request arrives,
  so tools never receive a None pool.
- ``project_id`` is included here rather than only in the query text so tools
  can scope every query without having to parse the natural-language prompt.
- ``embedding_model`` is a warm-loaded BAAI/bge-small-en-v1.5 SentenceTransformer
  instance stored on ``app.state``.  Kept as ``Any`` because the concrete
  SentenceTransformer type is not a hard import in production; tools that use
  it check for None and degrade gracefully.
- ``reranker`` is a warm-loaded CrossEncoder (ms-marco-MiniLM-L-6-v2) that
  re-scores (query, chunk) pairs with raw logits.  None if the model failed
  to load; search_documents degrades to returning raw Qdrant scores instead.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg
from neo4j import AsyncDriver
from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)


@dataclass
class AgentDeps:
    """Container for all shared resources injected into every agent tool call.

    Attributes
    ----------
    pg_pool:
        asyncpg connection pool pointing at PostGIS via PgBouncer.
    qdrant_client:
        Async Qdrant client for vector similarity search.
    neo4j_driver:
        Async Neo4j driver for knowledge-graph traversal.
    project_id:
        UUID string of the project scoping this query.  Every tool filters
        results to this project so the LLM never sees data from other projects.
    embedding_model:
        Warm-loaded BAAI/bge-small-en-v1.5 SentenceTransformer instance
        (or None if the model failed to load at startup).  Tools that embed
        query text at query time (search_documents) check for None and fall
        back gracefully.
    reranker:
        Warm-loaded CrossEncoder instance (cross-encoder/ms-marco-MiniLM-L-6-v2)
        for Layer 1 precision reranking.  None if the model failed to load;
        search_documents falls back to raw Qdrant cosine ordering instead.
    """

    pg_pool: asyncpg.Pool
    qdrant_client: AsyncQdrantClient
    neo4j_driver: AsyncDriver
    project_id: str
    # Module 9 Chunk 9.3 — workspace_id GUC scoping. Optional because some
    # callers (single-tenant Dagster ingestion, admin scripts) intentionally
    # don't carry workspace context. When set, acquire_scoped() emits
    # SET LOCAL georag.workspace_id alongside georag.project_id so the
    # workspace-scoped RLS policies on silver.evidence_items, answer_runs,
    # answer_retrieval_items, answer_citation_items, answer_citation_spans,
    # document_revisions, document_passages, and message_feedback fire.
    workspace_id: str | None = None
    embedding_model: Any = None  # SentenceTransformer (BAAI/bge-small-en-v1.5)
    reranker: Any = None  # CrossEncoder (cross-encoder/ms-marco-MiniLM-L-6-v2)
    # B2 — pooled clients; Any-typed so missing imports (non-anthropic deploys)
    # don't fail module load. Orchestrator falls back to lazy construction
    # when either is None (backward-compat path for tests and non-anthropic
    # backends).
    redis_client: Any = None  # redis.asyncio.Redis
    anthropic_client: Any = None  # anthropic.AsyncAnthropic
    # P1 #13 — pooled httpx.AsyncClient for the OpenAI-compatible backend.
    # Reused across `_call_openai_compatible_llm` invocations so we don't
    # pay TLS handshake + connection-pool warmup per call. None means the
    # caller falls back to ad-hoc `async with httpx.AsyncClient(...)`
    # (test path / pre-pool deploys).
    openai_http_client: Any = None  # httpx.AsyncClient
    # B7 — user scope attached by the JWT auth dependency. None when absent
    # (legacy X-Service-Key only); tools that need user-level RBAC check
    # for None and degrade gracefully.
    user_id: str | None = None
    user_roles: tuple[str, ...] = ()

    @contextlib.asynccontextmanager
    async def acquire_scoped(self):
        """Acquire a pooled PG connection inside a project-scoped transaction.

        DB review (Medium — RLS) — pairs with the GUC-aware row-security
        policies on silver.collars / silver.samples. The migration
        2026_04_17_120200_replace_toothless_rls_with_guc_aware_policies.php
        installs policies that read `current_setting('georag.project_id', true)`
        and admit:
          * every row when the GUC is unset (single-tenant / Dagster)
          * only matching project_id rows when the GUC is set

        This method is the canonical "set the GUC" path. It opens a
        transaction (required by SET LOCAL semantics — and required by
        PgBouncer transaction-pool mode anyway), sets the GUC, and yields
        the connection. Commit / rollback happens automatically via the
        asyncpg transaction context manager.

        Tools that want hardened multi-tenant retrieval should switch from:

            async with deps.pg_pool.acquire() as conn:
                rows = await conn.fetch(sql, *args)

        to:

            async with deps.acquire_scoped() as conn:
                rows = await conn.fetch(sql, *args)

        When MULTI_TENANT_ENFORCEMENT_ENABLED is False (default), the GUC
        is intentionally NOT set — single-tenant deploys keep their existing
        behaviour. When True, the GUC is set to deps.project_id and any
        cross-project leak in a tool's WHERE clause is silently caught by
        the RLS policy.
        """
        from app.config import settings  # noqa: PLC0415

        async with self.pg_pool.acquire() as conn:
            async with conn.transaction():
                # DB review (Medium — runaway-query guards). Per-transaction
                # statement_timeout is the FastAPI-only safety net for the
                # case where asyncio.wait_for cancels the asyncpg call but
                # Postgres is mid-call into libgeos and only checks for
                # cancellation at op-boundaries. SET LOCAL keeps it scoped
                # to this transaction so Dagster ingestion (which uses a
                # different connection / different acquire path entirely)
                # is never affected.
                #
                # Value matches 2× TIMEOUT_POSTGIS_S so the asyncio cancel
                # always fires first under normal operation; this only
                # triggers when the cancel can't propagate.
                stmt_timeout_ms = int(settings.TIMEOUT_POSTGIS_S * 1000) * 2
                await conn.execute(
                    f"SET LOCAL statement_timeout = '{stmt_timeout_ms}ms'"
                )

                if settings.MULTI_TENANT_ENFORCEMENT_ENABLED and self.project_id:
                    # SET LOCAL is scoped to the surrounding transaction —
                    # safe under PgBouncer transaction pooling because the
                    # backend connection isn't returned to the pool until
                    # COMMIT/ROLLBACK fires.
                    # asyncpg refuses to parameterise SET because the GUC
                    # name isn't a placeholder. Quote the value defensively
                    # — UUIDs only contain [0-9a-f-] so this is safe; we
                    # also guard with a regex.
                    pid = str(self.project_id).strip()
                    if not _UUID_RE.match(pid):
                        raise ValueError(
                            f"acquire_scoped: refusing to set non-UUID project_id={pid!r}"
                        )
                    await conn.execute(f"SET LOCAL georag.project_id = '{pid}'")

                    # Module 9 Chunk 9.3 — workspace_id GUC. Set when the
                    # caller carries workspace context so RLS policies on
                    # workspace-scoped silver tables (evidence_items,
                    # answer_runs, document_revisions, etc.) admit only
                    # matching rows. When workspace_id is None the GUC
                    # stays unset and the IS NULL escape-hatch keeps the
                    # single-tenant / admin path unchanged.
                    if self.workspace_id:
                        wid = str(self.workspace_id).strip()
                        if not _UUID_RE.match(wid):
                            raise ValueError(
                                f"acquire_scoped: refusing to set non-UUID workspace_id={wid!r}"
                            )
                        await conn.execute(
                            f"SET LOCAL georag.workspace_id = '{wid}'"
                        )
                yield conn


# UUID v4-ish regex — matches the canonical 8-4-4-4-12 hex form. Kept
# permissive (any 1-5 UUID variant) but rejects anything with quotes,
# semicolons, or whitespace that could break out of the SET LOCAL string.
import re  # noqa: E402

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ToolContext:
    """Typed context shim for orchestrator-dispatched tool calls.

    The deterministic orchestrator bypasses Pydantic AI's agent runtime
    and calls tool functions directly. Tools expect a context object with
    a ``.deps`` attribute (AgentDeps). This class provides that contract
    explicitly instead of using ad-hoc anonymous classes.

    Usage in orchestrator:
        ctx = ToolContext(deps)
        result = await query_spatial_collars(ctx, project_id=deps.project_id, ...)
    """

    __slots__ = ("deps",)

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps
