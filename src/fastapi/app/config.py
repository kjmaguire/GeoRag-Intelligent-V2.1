"""Application settings loaded from environment variables.

Uses pydantic-settings so every value is validated at startup. If a required
variable is missing the application will fail fast with a clear error rather
than crashing at the first database call.

Section 06 timeout constants are also centralised here so they can be imported
by any module without re-defining magic numbers.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# R13 — minimum key length for HS256 HMAC. RFC 7518 §3.2 requires the HMAC
# key be at least as long as the hash output (256 bits = 32 bytes) for
# SHA-256. Below this, PyJWT emits InsecureKeyLengthWarning and the key is
# brute-forceable. We fail startup rather than emit a warning a human might
# miss in the logs.
_SERVICE_KEY_MIN_BYTES = 32


class Settings(BaseSettings):
    """GeoRAG FastAPI service configuration.

    All values are read from environment variables (or a .env file at the
    project root if present). Variable names are case-insensitive.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # -------------------------------------------------------------------------
    # Service-to-service auth
    # -------------------------------------------------------------------------

    FASTAPI_SERVICE_KEY: str

    # V1.5-03 — kid-based JWT key rotation. The PRIMARY key + kid match what
    # Laravel currently mints. During a rotation window, set PREVIOUS to the
    # outgoing key + its kid; FastAPI accepts both until Laravel cuts over.
    # See ops/runbooks/secret-rotation.md § FASTAPI_SERVICE_KEY for the
    # operator playbook.
    FASTAPI_SERVICE_KEY_KID: str = "primary"
    FASTAPI_SERVICE_KEY_PREVIOUS: str = ""
    FASTAPI_SERVICE_KEY_PREVIOUS_KID: str = ""

    # Phase 3 Step 3 — per-flow JWT signing secret for Kestra → FastAPI
    # integrations bridge. Distinct from FASTAPI_SERVICE_KEY so rotation
    # of one doesn't disturb the other. 32-byte minimum per HS256.
    KESTRA_FLOW_JWT_SECRET: str = ""

    @field_validator("FASTAPI_SERVICE_KEY")
    @classmethod
    def _validate_service_key_length(cls, v: str) -> str:
        # R13 — fail fast on weak keys rather than surface PyJWT's
        # InsecureKeyLengthWarning at runtime, which is easy to miss in logs.
        # The key signs JWTs (HS256) and gates X-Service-Key auth; both paths
        # depend on it being cryptographically strong. 32 bytes matches the
        # SHA-256 output size per RFC 7518 §3.2.
        key_bytes = len(v.encode("utf-8"))
        if key_bytes < _SERVICE_KEY_MIN_BYTES:
            raise ValueError(
                f"FASTAPI_SERVICE_KEY is {key_bytes} bytes — must be >= "
                f"{_SERVICE_KEY_MIN_BYTES} for HS256 JWT signing. "
                f"Generate a new one with: "
                f"python3 -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        return v

    # -------------------------------------------------------------------------
    # FastAPI runtime — DoS surface protection + observability surface
    # -------------------------------------------------------------------------
    # Logging level — overridable via LOG_LEVEL env var. Defaults to INFO.
    # drift-audit 2026-05-13: the main.py hasattr(settings, "LOG_LEVEL") guard
    # was silently dead because the field was never declared here.  Adding the
    # field makes the env-var override actually work.
    LOG_LEVEL: str = "INFO"

    # -------------------------------------------------------------------------
    # Sentry — error tracking, traces, profiling, logs
    # -------------------------------------------------------------------------
    # Same Sentry project as the Laravel side ("georag"). Leave blank to
    # disable the SDK entirely (sentry_sdk.init is gated on this in main.py).
    # Sample rates are 1.0 in dev for full visibility; drop to ~0.1 in prod.
    SENTRY_DSN: str = ""
    SENTRY_TRACES_SAMPLE_RATE: float = 1.0
    SENTRY_PROFILES_SAMPLE_RATE: float = 1.0
    SENTRY_ENABLE_LOGS: bool = True
    SENTRY_RELEASE: str = ""
    SENTRY_ENVIRONMENT: str = "development"

    # FastAPI review #1 — cap request body size. Starlette default is
    # unbounded; a 10 GB POST OOMs the worker before Pydantic ever runs.
    # 1 MiB is generous for our largest legitimate body (~10 KB chat query).
    MAX_REQUEST_BODY_BYTES: int = 1_048_576  # 1 MiB

    # FastAPI review #2 — global per-request timeout backstop.
    # Enforced by `_GlobalTimeout` middleware. SSE stream endpoints opt
    # out via the Accept header check (they own their own deadline via
    # TIMEOUT_GATHER_S=8). 30 s is generous for any non-streaming work.
    REQUEST_TIMEOUT_S: float = 30.0

    # FastAPI review #3 — gate the OpenAPI docs (Swagger UI + ReDoc +
    # raw /openapi.json) behind a flag. They're convenient in dev but
    # leak the full request schema + auth-claim shapes in prod.
    OPENAPI_DOCS_PUBLIC: bool = True

    # FastAPI review #9 — rate limit the chat endpoint. Off by default
    # because single-tenant deploys don't need it (Laravel front door
    # is the natural rate-limiting boundary). Flip on for multi-tenant
    # where one runaway tenant could DoS shared FastAPI capacity.
    # Format follows slowapi/limits ("60/minute", "5/second", etc.).
    RATE_LIMIT_ENABLED: bool = False
    RATE_LIMIT_DEFAULT: str = "60/minute"
    RATE_LIMIT_QUERIES: str = "20/minute"  # the expensive endpoint

    # -------------------------------------------------------------------------
    # PostgreSQL / PgBouncer
    # -------------------------------------------------------------------------

    POSTGRES_HOST: str = "pgbouncer"
    POSTGRES_PORT: int = 6432
    POSTGRES_DB: str = "georag"
    POSTGRES_USER: str = "georag"
    POSTGRES_PASSWORD: str

    # -------------------------------------------------------------------------
    # Neo4j
    # -------------------------------------------------------------------------

    NEO4J_HOST: str = "neo4j"
    NEO4J_PORT: int = 7687
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "neo4j"

    # -------------------------------------------------------------------------
    # Qdrant
    # -------------------------------------------------------------------------

    QDRANT_HOST: str = "qdrant"
    QDRANT_PORT: int = 6333
    # Qdrant review #9 — optional API key for prod.
    # Leave blank in dev (port 6333 is not exposed externally and only
    # the internal Docker network reaches Qdrant). In prod, set
    # QDRANT_API_KEY in .env — the value is passed to both the FastAPI
    # AsyncQdrantClient AND the Qdrant container itself via compose.
    QDRANT_API_KEY: str = ""

    # -------------------------------------------------------------------------
    # Redis
    # -------------------------------------------------------------------------

    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str = ""

    # -------------------------------------------------------------------------
    # LLM — supports an OpenAI-compatible vLLM backend + native Anthropic:
    #   vllm       (default for dev + prod) → vLLM serving Qwen3-14B-AWQ
    #                                on the dev workstation A4500, scaled
    #                                up on prod hardware. Single inference
    #                                runtime per master-plan §12. Ollama
    #                                support removed 2026-05-17.
    #   anthropic  (prod fallback) → Anthropic API (Claude Opus 4.7, Sonnet 4.6,
    #                                Haiku 4.5). Native SDK — enables prompt
    #                                caching, adaptive thinking, priority tier,
    #                                and the 300k-output batch beta that the
    #                                OpenAI-compatible proxy path cannot reach.
    #
    # Switch by setting LLM_BACKEND in .env. vLLM uses the OpenAI-compatible
    # /v1/chat/completions endpoint; Anthropic uses its native messages API.
    # -------------------------------------------------------------------------

    LLM_BACKEND: str = "vllm"  # "vllm" | "anthropic"

    # Primary OpenAI-compatible target. With LLM_BACKEND=vllm (the only
    # local-LLM option post-cutover), the VLLM_* values below are
    # authoritative via `effective_llm_url` / `effective_llm_model`.
    # LLM_PRIMARY_URL / LLM_PRIMARY_MODEL remain as the canonical OpenAI-shaped
    # target indirection so failover paths can be retargeted via env without
    # touching code.
    LLM_PRIMARY_URL: str = "http://vllm:8000/v1"
    LLM_PRIMARY_MODEL: str = "Qwen/Qwen3-14B-AWQ"

    # vLLM — used when LLM_BACKEND=vllm.
    #
    # Default model: Qwen/Qwen3-14B-AWQ — community AWQ
    # quant of the 2507 release using real AWQ format (`quant_method: awq`,
    # `version: gemm`, INT4/group-128). The Qwen team did not publish an
    # official AWQ for 2507 (only FP8). Other community "AWQ" repos
    # (cyankiwi, stelterlab) use compressed-tensors `pack-quantized`
    # internally despite the name — those would need
    # `--quantization compressed-tensors`, NOT `awq_marlin`.
    #
    # Sized to fit the dev workstation's RTX A4500 (20 GB, compute 8.6 —
    # Ampere). FP8 is not viable on Ampere (no native; emulation is slow);
    # BF16 30B is too large to fit. AWQ INT4 weights are ~17 GB, leaving
    # ~1.5-2 GB for KV cache at max_model_len=8192 with
    # gpu_memory_utilization=0.92.
    #
    # `VLLM_QUANTIZATION` is the value passed to `--quantization` on the vLLM
    # serve command. `awq_marlin` selects the Marlin INT4 kernels (Ampere-
    # safe, near-FP16 throughput). `gptq_marlin` is the equivalent for GPTQ
    # quants. Leave unset / empty when serving an unquantized BF16 model.
    #
    # `VLLM_MAX_MODEL_LEN` is the per-request prompt+completion ceiling
    # passed to `--max-model-len`. Constrained by KV-cache budget = (gpu mem
    # × VLLM_GPU_MEMORY_UTILIZATION) − weights. On A4500 with AWQ 30B this
    # is roughly 8K; raise on hardware with more VRAM.
    #
    # `VLLM_GPU_MEMORY_UTILIZATION` is the fraction of GPU VRAM vLLM may
    # use for weights + KV cache. 0.92 is the realistic ceiling on the
    # A4500 *as a desktop card* — Windows desktop compositing (explorer,
    # Edge, Office, Docker Desktop, etc.) permanently holds ~1-1.5 GiB of
    # VRAM that the GPU compositor won't release to a non-display
    # application. Headless A4500 hardware can push to 0.95+. Lower
    # further (0.85-0.90) if embeddings or reranker share the device.
    VLLM_URL: str = "http://vllm:8000/v1"
    VLLM_MODEL: str = "Qwen/Qwen3-14B-AWQ"
    VLLM_QUANTIZATION: str = "awq_marlin"
    VLLM_MAX_MODEL_LEN: int = 8192
    VLLM_GPU_MEMORY_UTILIZATION: float = 0.92
    # Per-request output ceiling. Mirrors LLM_MAX_OUTPUT_TOKENS for the
    # OpenAI-compat path; keep aligned so the budget is consistent across
    # backends.
    VLLM_MAX_TOKENS: int = 4096
    VLLM_TEMPERATURE: float = 0.1

    # Anthropic native backend — only used when LLM_BACKEND=anthropic
    ANTHROPIC_API_KEY: str = ""
    # Default to Opus 4.7 (strongest agentic coding, 1M ctx). Swap to
    # claude-sonnet-4-6 for cheaper/faster synthesis, or claude-haiku-4-5
    # for the fast-path LLM summary when the classifier is confident.
    ANTHROPIC_MODEL: str = "claude-opus-4-7"
    ANTHROPIC_MAX_OUTPUT_TOKENS: int = 4096

    # Ollama review #5 — `num_predict` cap for the OpenAI-compatible
    # path. Default in Ollama is -1 (unlimited) — a small model that
    # gets stuck in a repetition loop will generate forever and burn
    # the FastAPI 8 s deadline. Match the Anthropic ceiling so the
    # output budget is consistent across backends.
    LLM_MAX_OUTPUT_TOKENS: int = 4096

    # Qwen3 sampling parameters — published recommendations from the Qwen
    # team for the 3.x family. Two regimes:
    #   thinking ON  → temperature=0.7, top_p=0.8,  top_k=20, min_p=0
    #   thinking OFF → temperature=0.7, top_p=0.8,  top_k=20, presence_penalty=1.5
    # The presence_penalty on the thinking-off path mitigates Qwen3's
    # tendency to slip into repetition loops on long structured outputs.
    # Values flow into the `options` block of the OpenAI-compatible call.
    QWEN3_TOP_P: float = 0.8
    QWEN3_TOP_K: int = 20
    QWEN3_MIN_P: float = 0.0
    QWEN3_PRESENCE_PENALTY_NO_THINK: float = 1.5

    # When thinking is ON, reasoning trace tokens are written to the
    # `reasoning` field but still draw from the response token budget on
    # Ollama 0.21.0. Bump the per-call num_predict by this much on
    # free-text-with-thinking paths so the visible answer doesn't get
    # truncated when Qwen3 thinks heavily (~1-2K reasoning trace typical).
    # Set to 0 to disable the bump.
    LLM_MAX_THINKING_TOKENS: int = 2048
    # Prompt caching saves ~90% input cost + ~60-80% first-token latency
    # on cached reads. Cacheable boundary is the static system prompt;
    # per-request context + query are not cached.
    ANTHROPIC_ENABLE_PROMPT_CACHING: bool = True
    # Priority tier buys guaranteed throughput at a cost premium. Leave off
    # for development; turn on in production if 429s from standard tier bite.
    ANTHROPIC_USE_PRIORITY_TIER: bool = False

    # Legacy OpenAI-compatible fallback (pre-Anthropic integration). Kept for
    # back-compat when users already configured LLM_FALLBACK_URL against an
    # OpenAI-shaped proxy. For new installs, prefer LLM_BACKEND=anthropic.
    LLM_FALLBACK_ENABLED: bool = False
    LLM_FALLBACK_URL: str = ""
    LLM_FALLBACK_MODEL: str = ""
    LLM_FALLBACK_API_KEY: str = ""

    # R9 — bounded escalation via LLM query rephrasing. When a query
    # hits the "classifier_fallback + all tools empty" signature, the
    # orchestrator asks the LLM for up to MAX_REPHRASINGS alternative
    # phrasings and retries the deterministic tool dispatch on each
    # until one returns something. Latency cost is one extra LLM
    # round-trip + up to N retry passes of tool fan-out. Disable to
    # revert to the old straight-through "empty is empty" behaviour.
    AGENTIC_ESCALATION_ENABLED: bool = True
    AGENTIC_ESCALATION_MAX_REPHRASINGS: int = 2

    # LLM-based classifier fallback tier (→ A grade).
    # When the keyword classifier hits classifier_fallback, ask a FAST-tier
    # LLM to re-classify BEFORE the deterministic fan-out runs. Recovers
    # queries the keyword set can't match without escalating all the way
    # to the rephrasing retry. Off by default only if operators want to
    # isolate pure keyword routing during an evaluation pass.
    LLM_CLASSIFIER_FALLBACK_ENABLED: bool = True

    # R9-full — second-tier Pydantic AI agentic escalation. Fires ONLY when
    # both the deterministic dispatch AND the R9-lite rephrasing retry
    # returned empty. Off by default because the signal-harvesting
    # dashboard (Phase 4 #1) needs to show the R9-lite success rate drop
    # below ~50% before this tier adds net value. Turn on per-deploy via
    # env when telemetry justifies it.
    AGENTIC_FULL_ESCALATION_ENABLED: bool = False
    # §04p Phase 2.B-i — raised from 3 to 8 to budget for PDF chaining.
    # PDF chains like find_legends → crop_region → ocr_region → summarize
    # need at least 4 retrieval calls before verify_numerical_claim.
    # Non-PDF queries pay nothing extra: the agent stops once it has enough
    # context regardless of remaining budget.
    # Override per-deploy via AGENTIC_MAX_TOOL_CALLS env var.
    AGENTIC_MAX_TOOL_CALLS: int = 8
    # P1 #11 — verify_numerical_claim is registered alongside the retrieval
    # tools but should NOT eat into the discovery budget. Give it dedicated
    # headroom so a verification-happy model can still explore.
    # Pydantic AI's UsageLimits.tool_calls_limit is global, so the agent's
    # actual ceiling is AGENTIC_MAX_TOOL_CALLS + this value.
    AGENTIC_MAX_VERIFY_CALLS: int = 3
    AGENTIC_TIMEOUT_S: float = 10.0

    # P1 #14 — global per-query LLM-call cap. A single user query can
    # invoke the LLM many times: classifier escalation, query rephrasing,
    # the primary synthesis, retry-on-validation-failure, and one-shot
    # failover. Without a global ceiling a pathological query (or a buggy
    # retry loop) can rack up dozens of API calls — wasting budget AND
    # exceeding the request deadline.
    #
    # Tuned for the deepest-but-still-reasonable path:
    #   1 keyword classifier escalation (LLM_CLASSIFIER_FALLBACK_ENABLED)
    # + 1 primary synthesis
    # + 2 typed-output validation retries (MAX_RETRIES)
    # + 1 one-shot failover
    # + 1 follow-ups generation
    # = 6 calls
    # Default 8 leaves headroom; lower for cost-sensitive deploys.
    MAX_LLM_CALLS_PER_QUERY: int = 8

    # P1 #9 — Logfire / OpenTelemetry instrumentation for the Pydantic AI
    # agent. Logfire is the Pydantic team's OTel provider; it auto-traces
    # every agent run (system prompt, tool calls, tool returns, retries)
    # and emits spans either to Pydantic's hosted backend (LOGFIRE_TOKEN)
    # OR to a local OTel collector (LOGFIRE_OTEL_ENDPOINT). Default off so
    # no surprise outbound traffic.
    #
    # Enable for production-grade triage:
    #   LOGFIRE_ENABLED=true
    #   LOGFIRE_TOKEN=<pylogfire_xxx>            # hosted backend OR
    #   LOGFIRE_OTEL_ENDPOINT=http://tempo:4317  # local collector
    #
    # When LOGFIRE_TOKEN is set we send spans to logfire.pydantic.dev.
    # When LOGFIRE_OTEL_ENDPOINT is set we send to that OTLP endpoint
    # (Tempo / Jaeger / Honeycomb / Grafana Cloud) via the OTel exporter.
    # When both are unset BUT LOGFIRE_ENABLED=true we configure with
    # send_to_logfire=False so spans are still created locally for
    # in-process inspection — useful for `logfire.span(...)` debugging
    # without shipping data anywhere.
    LOGFIRE_ENABLED: bool = False
    LOGFIRE_TOKEN: str = ""
    LOGFIRE_OTEL_ENDPOINT: str = ""
    LOGFIRE_SERVICE_NAME: str = "georag-fastapi"
    LOGFIRE_ENVIRONMENT: str = "dev"

    # Langfuse — LLM-call observability (Phase 5 follow-up, 2026-05-19).
    # Keys were already in .env but the SDK was never instantiated. The
    # client is initialised in app/main.py lifespan; missing values leave
    # langfuse_client=None and the orchestrator no-ops around it.
    LANGFUSE_HOST: str = ""
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""

    # System-prompt routing (C5). When enabled, the orchestrator picks a
    # task-specific prompt variant (NUMERIC / NARRATIVE / DEFAULT) based on
    # classifier output. Variants share a common preamble so Anthropic
    # prompt-caching stays hot.
    SYSTEM_PROMPT_ROUTING_ENABLED: bool = True

    # Freshness ranking (Eval 01 L6 follow-up, 2026-05-20). When > 0, the
    # fusion layer demotes public_geo candidates whose ingested_at is older
    # than the workspace's current data_version timestamp. 0.0 = no-op
    # (safe default for transitional deploys); 0.2 = mild demote;
    # 1.0 = effectively drop stale public_geo. Operator-tunable.
    FRESHNESS_RANKING_WEIGHT: float = 0.0

    # Model-tier routing (B1). When enabled, the orchestrator picks a model
    # per query based on classifier output: factoid lookups go to FAST,
    # narrative synthesis to STANDARD, and multi-hop / retries to DEEP.
    # Set MODEL_ROUTING_ENABLED=False to pin every query to DEEP (the
    # pre-B1 behaviour) for A/B comparison against the golden set.
    MODEL_ROUTING_ENABLED: bool = True
    MODEL_TIER_FAST: str = "claude-haiku-4-5"
    MODEL_TIER_STANDARD: str = "claude-sonnet-4-5"
    MODEL_TIER_DEEP: str = "claude-opus-4-7"
    # R11 — hard-fail when the orchestrator is asked to run on Anthropic but
    # the pooled AsyncAnthropic client wasn't attached at startup. Set to
    # False only during bootstrapping (tests, mid-migration deploys) when
    # the pooled client may legitimately be absent. In production we want a
    # loud failure here rather than silent per-call construction churn.
    REQUIRE_POOLED_ANTHROPIC_CLIENT: bool = True

    # Failover target when an Anthropic request hits 429/529/timeout.
    #   "downshift" — try the next-lower Anthropic tier (DEEP→STANDARD→FAST).
    #   "local_llm" — switch backends to the local vLLM endpoint.
    #   None        — no failover; surface the error.
    LLM_BACKEND_FALLBACK: str | None = "downshift"

    # Max-marginal-relevance (B4) — de-dupes near-identical document chunks
    # that the cross-encoder reranker keeps when the same resource paragraph
    # appears in multiple report amendments. λ trades relevance against
    # diversity: 1.0 = pure relevance (off); 0.5 = balanced; 0.7 = favour
    # relevance but still prune clear duplicates (default). Set
    # MMR_ENABLED=False to disable if the golden-set baseline shows MMR
    # hurts recall on your corpus.
    MMR_ENABLED: bool = True
    MMR_LAMBDA: float = 0.7

    # Document-scope version (B5). Bump to invalidate all cached RAG responses
    # when the document-retrieval scope policy changes — e.g., switching
    # Qdrant from cross-project to per-project filtering in tools.search_documents.
    # The version is folded into the response cache key so a policy flip
    # cleanly invalidates stale cross-project answers instead of waiting for
    # the 5-minute TTL. Also bumped when the reranker or embedding model
    # changes and you want to force a cold-start.
    DOCUMENT_SCOPE_VERSION: int = 1

    # P0 #1 — Qdrant project_id scoping for search_documents.
    # `search_documents` targets the `georag_reports` collection. Historically
    # NI 43-101 filings are public and treated cross-project, but operators
    # can ingest proprietary / customer-uploaded reports into the same
    # collection — for those deployments we must be able to restrict document
    # retrieval to the requesting project.
    #
    # Modes:
    #   cross_project       — no filter (default; preserves current behaviour)
    #   project_or_public   — filter: project_id == <requested> OR
    #                         project_id missing OR project_id == "public".
    #                         Recommended after re-indexing with project_id
    #                         stamped on proprietary reports.
    #   strict              — filter: project_id == <requested> only.
    #                         Rejects any chunk without a project_id — will
    #                         return zero results until the indexer stamps
    #                         project_id on every point.
    #
    # Flip the default to `project_or_public` after running the index_reports
    # asset with the project_id payload patch and bumping
    # DOCUMENT_SCOPE_VERSION to invalidate the RAG response cache.
    QDRANT_DOCUMENT_PROJECT_SCOPE: str = "cross_project"

    # ADR-0010 — silver.document_passages is the canonical chunked-content
    # corpus. When True, `search_documents` reads from the new
    # `georag_chunks` collection (fed by the Dagster
    # `index_document_passages` asset) instead of the legacy
    # `georag_reports` collection (fed by `index_reports`). Hard flag
    # flip per Kyle 2026-05-27 — no transition / shadow mode.
    #
    # Flipped to True on 2026-05-28 (overnight ADR-0010 Session C) after
    # candidate benchmark on georag_chunks (7,065 points, full backfill)
    # matched the georag_reports baseline at pass_rate=0.20 (delta +0.000,
    # within ±1pp tolerance per Kyle's locked retirement criterion).
    # Bench artifacts:
    #   bench_results/adr0010-baseline-20260528T061258Z.json
    #   bench_results/adr0010-candidate-20260528T104735Z.json
    # Per the "stage + hold for morning" rule, this default flip is
    # committed but the legacy georag_reports collection drop + the
    # index_reports asset retirement are deferred to Kyle's review.
    # The collection-name swap is the only behavioural difference:
    # payload shape is kept compatible via `report_id`/`page` aliases on
    # the new collection's payload (see _build_payload in
    # index_document_passages).
    RETRIEVAL_USE_DOCUMENT_PASSAGES: bool = True

    # P0 #4 — multi-tenant project boundary enforcement.
    # When True, FastAPI routes that accept a JWT refuse to service the
    # request if:
    #   * the Authorization header is missing / not a Bearer JWT, OR
    #   * the JWT's `project_id` claim does not equal the request body's
    #     `project_id`, OR
    #   * the JWT has no `project_id` claim at all.
    #
    # When False (default — graceful rollout), the check is a soft warning:
    # we log the mismatch and honour the body's project_id. This lets legacy
    # Laravel deploys that haven't shipped the JWT minter yet continue to
    # work against the current FastAPI build.
    #
    # Flip to True once every Laravel deploy in your environment is signed
    # up through the FastApiJwtMinter path. Do NOT flip in a multi-customer
    # deployment until you have verified the minter is in place everywhere
    # — otherwise legitimate requests will start returning 403.
    #
    # Single-tenant deployments can safely leave this False; the JWT flow
    # still signs every request and project ownership is enforced at the
    # Laravel layer before the JWT is minted. The main benefit of flipping
    # it on is defence-in-depth in multi-customer deployments.
    # Module 9 Chunk 9.4 (A2-03) — flipped to True. Multi-tenant deployments
    # MUST enforce JWT-vs-body equality. Solo deployments must opt out via
    # SINGLE_TENANT_MODE=True; a model-validator below refuses to start the
    # service if both flags are False (loud failure beats silent insecurity).
    MULTI_TENANT_ENFORCEMENT_ENABLED: bool = True

    # Explicit single-tenant escape hatch. When True, MULTI_TENANT_ENFORCEMENT_ENABLED
    # may be False; when False, MULTI_TENANT_ENFORCEMENT_ENABLED must be True.
    # This flag also relaxes the X-Workspace-Id-only path in workspace_resolution.
    SINGLE_TENANT_MODE: bool = False

    # Module 6 Phase B Chunk 2 — Citation span resolver (2026-04-22).
    # When true, the orchestrator runs Stage 1 (evidence binding) before the
    # LLM call and Stage 2 (span resolver) after, writing rows to
    # silver.answer_citation_items + silver.answer_citation_spans.
    # The new colon-form system prompt variant ([DATA:N]) is selected when
    # this flag is on; the legacy dash-form prompt variants remain available
    # and are used when false.
    # Default false — Chunk 2 is staged; senior-reviewer approval required
    # before flipping. See docs/module-6-chunk-2-design.md.
    CITATION_SPAN_RESOLVER_ENABLED: bool = False

    # -------------------------------------------------------------------------
    # Phase 1 / Step 1.2 — OIUR answer schema rollout
    # -------------------------------------------------------------------------
    # When true, the orchestrator (a) appends OIUR output rules to the
    # selected system prompt so the LLM emits Observations / Interpretations /
    # Uncertainty / Recommended-actions H2 sections, and (b) the response
    # assembler parses that markdown into a ``GeoAnswer`` and attaches it to
    # ``GeoRAGResponse.geo_answer``. When false, behaviour is unchanged —
    # prompts and responses stay in the legacy flat-text shape.
    #
    # On parser failure the assembler logs a warning and leaves ``geo_answer``
    # as None — the flat ``text`` path remains the always-on fallback so
    # downstream consumers (Laravel bridge, chat UI) never break.
    #
    # See ``docs/phase1-oiur.md`` (added in Step 1.2b) for the rollout plan.
    GEO_ANSWER_OIUR_ENABLED: bool = False

    # -------------------------------------------------------------------------
    # Phase 2 / Step 2.3 — Agentic Retrieval v2 (§04j Track A.2)
    # -------------------------------------------------------------------------
    # When true, the orchestrator routes the query through the LangGraph
    # agentic-retrieval pipeline in ``app.agent.agentic_retrieval`` instead
    # of the manual tool dispatch in ``run_deterministic_rag``. Six intent
    # subgraphs (factual / synthesis / hypothesis / anomaly / uncertainty /
    # decision) each apply a different retrieval profile. Requires the
    # OIUR flag to be on for the answer-template variants to be emitted.
    #
    # Default false — Phase 2 lands behind this flag with a separate review
    # gate from Phase 1 because the entire query-dispatch path changes when
    # it flips on. See ``docs/phase2-agentic-retrieval.md`` for the rollout.
    AGENTIC_RETRIEVAL_V2_ENABLED: bool = False

    # -------------------------------------------------------------------------
    # Plan §3 — context preparation pipeline (§3b + §3c + §3f)
    # -------------------------------------------------------------------------
    # When true, ``assemble_node`` runs the typed ``EvidencePacket`` from
    # ``state.evidence_packet`` through
    # ``app.agent.context_prep.prepare_evidence_for_intent`` BEFORE building
    # the LLM context block. The pipeline:
    #
    #   1. annotate_evidence_packet_with_authority (§3b refresh ranks)
    #   2. rank_evidence_by_authority             (§3b sort)
    #   3. apply_source_diversity                 (§3c per-intent quota)
    #   4. enforce_token_budget                   (§3f drop loop)
    #
    # When false (the default), assemble_node falls back to reading
    # ``state.tool_results`` directly into the LLM context — the legacy
    # path that's been in production all of Phase 2. The flag exists so
    # the change can land BEHIND a gate while the golden-query baseline
    # is established (the LLM-context shape changes; we want to detect
    # answer-quality regressions before they hit prod).
    #
    # Requires AGENTIC_RETRIEVAL_V2_ENABLED=True to have any effect —
    # the legacy deterministic path doesn't build an EvidencePacket.
    #
    # Rollout: shadow → enable for synthetic eval set → enable for
    # power-user workspaces → general availability. See OVERNIGHT_LOG
    # §27 for the wiring details.
    CONTEXT_PREP_ENABLED: bool = False

    # -------------------------------------------------------------------------
    # Plan §4b/§4c — repair loop, Stage 1 (shadow mode)
    # -------------------------------------------------------------------------
    # When true, a `repair_shadow_node` runs after `validate_node`:
    #
    #   1. classify_guards(...) — typed codes from current state
    #   2. plan_repair(codes, prior_strategies=...) — the strategy list
    #      the orchestrator WOULD have attempted next
    #   3. Writes both onto state.repair_codes_observed +
    #      state.repair_strategy_history + state.repair_terminal_reason
    #
    # Crucially, shadow mode does NOT modify retrieval state and does NOT
    # re-issue the graph. It's pure telemetry — we record what the loop
    # would have done so we can size the cost/latency impact of turning
    # the full loop on later. See docs/architecture/repair_loop_spec.md
    # §8 Stage 1 for the rollout plan.
    #
    # Default false. Stage 1 → flip on for staging; Stage 4 (full loop)
    # gets its own flag once we have shadow telemetry to size it from.
    REPAIR_LOOP_SHADOW_ENABLED: bool = False

    # -------------------------------------------------------------------------
    # Plan §3e — multi-turn context resolution
    # -------------------------------------------------------------------------
    # When true, a `resolve_node` runs BEFORE `classify_node` and rewrites
    # the user's query using conversation history (passed in via
    # AgenticRetrievalState.history). Three reference classes are handled:
    #   1. Pronoun coreference ('it', 'its', 'they', 'their', 'that')
    #   2. Demonstrative reference ('the same hole', 'this property')
    #   3. Comparative reference ('the previous one', 'the first one')
    #
    # See docs/architecture/multi_turn_resolution_spec.md for the contract.
    # Default off — conversation history plumbing across Laravel + FastAPI
    # is the larger half of the wire; this flag exists so the FastAPI side
    # can ship first and the Laravel loader follows in a later commit.
    MULTI_TURN_RESOLUTION_ENABLED: bool = False

    # -------------------------------------------------------------------------
    # Plan §2c — entity-resolver shadow mode
    # -------------------------------------------------------------------------
    # When True, the agentic graph runs a shadow pass that resolves
    # extracted hole IDs through silver.entity_aliases and logs any
    # gaps. Pure telemetry — does NOT modify the answer path or the
    # query. Identical rollout shape as REPAIR_LOOP_SHADOW_ENABLED.
    #
    # Future Stage 4 of ADR-0009 will flip this to use the resolved
    # canonical names in retrieval; for now it just populates
    # silver.alias_gaps so the SME review queue catches misses.
    ENTITY_RESOLVER_SHADOW_ENABLED: bool = False

    # -------------------------------------------------------------------------
    # Plan §1b — parent-child chunker (greenfield)
    # -------------------------------------------------------------------------
    # When True, pdf_ingester._chunk_pages emits parent + child passage
    # pairs rather than the legacy flat narrative chunks. Children carry
    # parent_chunk_id pointing at a section-level parent that's the
    # concatenation of N contiguous children. The §3d expander (shipped
    # 2026-05-28 commit 7049e20) consumes the parent_chunk_id FK to
    # widen retrieval context — inert until this flag flips.
    #
    # Greenfield-only: existing 7,064 passages keep parent_chunk_id=NULL
    # forever. Re-ingest a doc to populate. See
    # docs/architecture/parent_child_chunker_spec.md for the design +
    # cost analysis + rollout staging.
    #
    # Default False. Rollout: dev validation → 1-week telemetry →
    # staging → prod.
    PARENT_CHUNKING_ENABLED: bool = False

    # Number of children grouped under one parent passage. N=3 yields
    # ~2,400 token parents (3 × 800 target child size), well under the
    # 8000-char cap enforced by parent_expansion.expand_parents_sync.
    # See spec §2 for the cost vs context-width tradeoff at N=3/4/5.
    PARENT_CHUNKING_GROUP_SIZE: int = 3

    # -------------------------------------------------------------------------
    # Plan §4b — repair loop, Stage 2 (terminal-only strategies)
    # -------------------------------------------------------------------------
    # When True, repair_shadow_node STAMPS the response with terminal-
    # strategy payloads when the dispatcher chose one:
    #   ASK_FOR_DISAMBIGUATION     → response.refusal_payload with
    #                                 reason_code + candidate list
    #   SURFACE_CONFLICT           → response.conflicting_evidence
    #                                 already populated; no extra stamp
    #   REQUEST_UNIT_CLARIFICATION → response.refusal_payload with
    #                                 reason_code = MISSING_ASSAY_UNITS
    #   REQUEST_DEPTH_CLARIFICATION → response.refusal_payload with
    #                                  reason_code = MISSING_DEPTH_INTERVAL
    #   REFUSE_OUT_OF_SCOPE        → response.refusal_payload with
    #                                 reason_code from the triggering code
    #
    # Loop-friendly strategies still shadow-only (no retrieval re-issue).
    # Stage 2 lights up the user-facing pickers/banners from real signals.
    REPAIR_LOOP_TERMINAL_ENABLED: bool = False

    # -------------------------------------------------------------------------
    # Plan §4b — repair loop, Stage 3 (low-cost LLM-only strategies)
    # -------------------------------------------------------------------------
    # When True, the orchestrator re-issues the LLM call (no extra
    # retrieval) with system_prompt suffix injection for:
    #   REPHRASE_NUMERIC_CLAIM      → suffix tells LLM to mark
    #                                  un-grounded numerics as ESTIMATED
    #   REQUEST_CITATION_RETRY      → suffix tightens citation
    #                                  requirement to one per claim
    #
    # Capped at REPAIR_LOOP_MAX_ATTEMPTS retries. Cost amplification:
    # +1 LLM call per failed query. Stage 3 telemetry sizes the impact
    # before Stage 4 (retrieval-side amplification).
    REPAIR_LOOP_LOWCOST_ENABLED: bool = False

    # -------------------------------------------------------------------------
    # Plan §4b — repair loop, Stage 4 (full retrieval-side strategies)
    # -------------------------------------------------------------------------
    # When True, the orchestrator applies retrieval-side strategies on
    # repair attempts: LOOSEN_FILTERS, BROADEN_KNN, ENABLE_FUZZY_ENTITY,
    # ADD_SPATIAL_BUFFER, TRANSFORM_CRS, INCREASE_GRAPH_DEPTH. Each
    # re-issues execute_node with modified state.retrieval_filters /
    # state.retrieval_profile.
    #
    # Cost amplification: +1-2 retrieval cycles per failed query. The
    # heaviest stage; only enable after Stages 2+3 baseline lands.
    REPAIR_LOOP_FULL_ENABLED: bool = False

    # Maximum total repair iterations per query (across all stages).
    # detect_death_loop short-circuits after 2 identical empty attempts;
    # this is the broader ceiling.
    REPAIR_LOOP_MAX_ATTEMPTS: int = 2

    # -------------------------------------------------------------------------
    # Phase G overnight — §10 Customer Support Cockpit outbound integrations
    # -------------------------------------------------------------------------
    # Kestra dispatch for support_packet bundles. When KESTRA_URL is unset,
    # the support_packet agent assembles + returns the bundle dict as before
    # (in-process, no outbound call). When set, the agent POSTs the bundle
    # to a Kestra flow execution endpoint so downstream operator workflows
    # (Slack notify, SeaweedFS archive, audit-trail attach, etc.) can run.
    #
    # KESTRA_URL example: "https://kestra.geo-rag.internal" — base only.
    # The agent appends `/api/v1/executions/{namespace}/{flowId}` per Kestra's
    # REST API contract.
    KESTRA_URL: str = ""
    KESTRA_FLOW_NAMESPACE: str = "georag.support"
    KESTRA_FLOW_ID: str = "support_packet_received"
    KESTRA_FLOW_AUTH_TOKEN: str = ""
    KESTRA_HTTP_TIMEOUT_S: float = 5.0

    # PagerDuty Events API v2 for escalation_routing. When the integration
    # key is empty, the agent returns its advisory recommendation only
    # (no outbound page). When set, the agent POSTs an Events v2 trigger
    # event keyed on the ticket_id (dedup_key) so re-routing the same ticket
    # updates the existing incident rather than creating duplicates.
    #
    # Integration key is the per-service routing key from PagerDuty's
    # service settings (Settings → Services → <service> → Integrations).
    # The default API URL points at PagerDuty's global Events v2 endpoint.
    PAGERDUTY_INTEGRATION_KEY: str = ""
    PAGERDUTY_API_URL: str = "https://events.pagerduty.com/v2/enqueue"
    PAGERDUTY_HTTP_TIMEOUT_S: float = 5.0

    # Retrieval cache — Phase H finally lit it up properly.
    #
    # History:
    #   Phase G overnight (2026-05-14) DISABLED the cache because the
    #     hit-path rehydration was design-incomplete. Every hit
    #     produced empty tool_results → "(no data retrieved)" context
    #     → model refused on sequential identical queries.
    #   Phase H (2026-05-15) SHIPPED the rehydration in
    #     `orchestrator/run_cache.py::rehydrate_tool_results`:
    #     - postgis candidates now serialise the full CollarRecord
    #       dataclass payload (was just `{store, canonical_id}`)
    #     - qdrant candidates already serialised DocumentChunk payloads
    #     - rehydration groups by store and rebuilds the original
    #       SpatialQueryResult / DocumentSearchResult dataclasses
    #     - neo4j candidates are skipped cleanly (no clean dataclass
    #       roundtrip for graph entity wrappers — the orchestrator's
    #       graph branch re-fires when needed)
    #
    # Default flipped to True. Empirical speedup on the smoke test:
    # first call 1.7s (cache miss + write), subsequent 0.85s
    # (cache hit + rehydrate + synthesize fresh) — half the latency
    # because retrieval + RRF + reranker are all skipped on hit.
    # Synthesis ALWAYS runs fresh per Global Invariant 12.
    #
    # Tests: tests/test_run_cache_rehydration.py (13 cases) +
    # explicit cache-hit smoke against the live LLM in
    # tests/test_cache_scope.py / test_cache_key_versioning.py.
    RETRIEVAL_CACHE_ENABLED: bool = True

    @model_validator(mode="after")
    def _validate_tenant_enforcement(self) -> Settings:
        """Module 9 Chunk 9.4 (A2-03) — refuse to start in an unsafe configuration.

        Allowed combinations:
          - MULTI_TENANT_ENFORCEMENT_ENABLED=True, SINGLE_TENANT_MODE=False  (production multi-tenant)
          - MULTI_TENANT_ENFORCEMENT_ENABLED=False, SINGLE_TENANT_MODE=True  (explicit single-tenant)
          - MULTI_TENANT_ENFORCEMENT_ENABLED=True, SINGLE_TENANT_MODE=True   (defensive — multi-tenant on, escape hatch acknowledged)

        Disallowed:
          - MULTI_TENANT_ENFORCEMENT_ENABLED=False, SINGLE_TENANT_MODE=False
            Loud failure beats silent insecurity. To opt out of multi-tenant
            enforcement on a solo deployment, set SINGLE_TENANT_MODE=True
            explicitly so the operator confirms the deployment shape.
        """
        if not self.MULTI_TENANT_ENFORCEMENT_ENABLED and not self.SINGLE_TENANT_MODE:
            raise ValueError(
                "MULTI_TENANT_ENFORCEMENT_ENABLED=False requires SINGLE_TENANT_MODE=True. "
                "Set SINGLE_TENANT_MODE=True explicitly if this is a solo deployment, "
                "or leave MULTI_TENANT_ENFORCEMENT_ENABLED=True for multi-tenant."
            )
        return self

    @property
    def effective_llm_url(self) -> str:
        """Return the LLM endpoint URL for OpenAI-compatible backends.

        Raises when LLM_BACKEND=anthropic, because the Anthropic path does
        not use a base URL — it goes through the native SDK.
        """
        if self.LLM_BACKEND == "vllm":
            return self.VLLM_URL
        if self.LLM_BACKEND == "anthropic":
            raise RuntimeError(
                "effective_llm_url is not applicable to LLM_BACKEND=anthropic. "
                "The Anthropic SDK manages its own endpoint."
            )
        return self.LLM_PRIMARY_URL

    @property
    def effective_llm_model(self) -> str:
        """Return the LLM model name based on the active backend."""
        if self.LLM_BACKEND == "vllm":
            return self.VLLM_MODEL
        if self.LLM_BACKEND == "anthropic":
            return self.ANTHROPIC_MODEL
        return self.LLM_PRIMARY_MODEL

    # -------------------------------------------------------------------------
    # Cross-database timeout constants (Section 06e)
    # These are in seconds and used by agent tools via asyncio.wait_for().
    # -------------------------------------------------------------------------

    TIMEOUT_POSTGIS_S: float = 5.0
    TIMEOUT_NEO4J_S: float = 3.0
    TIMEOUT_QDRANT_S: float = 2.0
    # Latency-fix follow-up — separate budget for the CPU-bound reranker.
    # Previously folded into TIMEOUT_QDRANT_S, which meant the bge-reranker
    # could blow the 2s budget and the wait_for would drop the entire
    # search_documents branch (incl. the Qdrant results that arrived fine
    # at ~70 ms). Sizing: bge-reranker-base on 10 threads needs ~450 ms
    # per pair at the model's 512-token max — i.e. ~4.5 s for 10 pairs
    # in the worst case (tokenisation included). 8 s gives headroom for
    # long-bodied chunks without letting a wedged reranker block the
    # whole request. Future lever: ONNX INT8 quantisation (~2-3x speedup).
    TIMEOUT_RERANKER_S: float = 8.0

    # Latency-fix follow-up — bge-reranker-base internally truncates to
    # max_length=512 tokens, but the tokeniser still walks the FULL body
    # to do that truncation. On 5 kB chunks that walk costs ~150 ms/pair
    # of wasted work. Pre-truncating to ~2000 chars (~500 tokens) at the
    # call site saves the per-pair tokenisation tail without changing
    # what the model actually sees.
    RERANKER_INPUT_CHAR_BUDGET: int = 2000

    TIMEOUT_REDIS_S: float = 0.5
    TIMEOUT_GATHER_S: float = 8.0  # hard deadline for parallel fan-out

    # -------------------------------------------------------------------------
    # Embedding and reranker model selection
    # -------------------------------------------------------------------------

    # bge-small-en-v1.5 replaces all-MiniLM-L6-v2 — same 384 dim, cosine, but
    # scores 10-15% higher on mineral/geological queries.  No Qdrant collection
    # recreation is needed; run scripts/reembed_qdrant.py to re-encode existing
    # points with the new model.
    EMBEDDING_MODEL_NAME: str = "BAAI/bge-small-en-v1.5"

    # ms-marco-MiniLM-L-6-v2 is a cross-encoder that re-scores (query, chunk) pairs
    # and produces raw logits.  Positive logits indicate a good match; the threshold
    # is set to 0.0 (any positive score) to avoid over-filtering.
    RERANKER_MODEL_NAME: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"

    # Number of chunks fetched from Qdrant before reranking (coarse retrieval).
    # Latency-fix follow-up — was 20; cold queries blew the search_documents
    # wait_for budget because the bge-reranker-base CPU pass on 20 candidates
    # consistently took >1.7s. Halving keeps the top-K with negligible
    # recall impact (bottom-10 ANN candidates almost never contain the
    # cited passage) and halves reranker wall time.
    RETRIEVAL_TOP_N: int = 10

    # Number of chunks to keep after reranking (fine retrieval, Layer 1 gate).
    # P1 #17 — bumped from 5 to 12 to give MMR a real candidate pool.
    # MMR is run downstream in the orchestrator with k=MAX_CONTEXT_DOC_CHUNKS;
    # at the old RERANKER_TOP_K=5 + MAX_CONTEXT_DOC_CHUNKS=5 the MMR step
    # was choosing 5-from-5, i.e. just sorting. With 12 candidates picked
    # by the reranker, MMR can actually drop near-duplicate amendments.
    RERANKER_TOP_K: int = 12

    # ── Plan §2a — retrieval-K decoupled from context-K ────────────────
    # These constants represent the plan §2a targets for a future
    # retrieval rework where each store contributes a wider candidate
    # pool that's then pooled + reranked + diversified. They are NOT
    # yet wired into the live agentic_retrieval pipeline (which still
    # consumes RETRIEVAL_TOP_N + RERANKER_TOP_K above). Downstream
    # wiring is tracked as the §2a M-effort follow-up (see
    # `docs/architecture/six_subgraphs_spec.md` §6 gap list).
    #
    # The values follow plan §2a Table A verbatim. Override via .env
    # for benchmarking without touching code.
    QDRANT_DENSE_TOP_K: int = 40
    QDRANT_SPARSE_TOP_K: int = 40
    POSTGIS_TOP_K: int = 50
    NEO4J_TOP_K: int = 30
    RERANK_CANDIDATES: int = 120     # pooled across all sources
    RERANK_TOP_K_PLAN_2A: int = 20   # reranker output (renamed to avoid
                                     # collision with the live RERANKER_TOP_K)
    FINAL_CONTEXT_GROUPS_MIN: int = 8
    FINAL_CONTEXT_GROUPS_MAX: int = 12

    # ── Plan §2b — dynamic temperature by query type ───────────────────
    # Read by the orchestrator's _call_openai_compatible_llm when the
    # intent classifier has produced a router_decision. Falls back to
    # the existing global default when intent is unknown. Plan §2b
    # verbatim values; tune per A/B benchmark results.
    #
    # Pydantic v2 doesn't infer types for plain dicts on a BaseSettings
    # subclass, so we declare it with an explicit ClassVar to keep it
    # off the env-loading path (it's not configurable via .env — code
    # change to update). The import is at module top — an in-class
    # ``from typing import ClassVar`` creates a non-annotated class
    # attribute that Pydantic's namespace inspector rejects.
    TEMPERATURE_BY_QUERY_TYPE: ClassVar[dict[str, float]] = {
        # Plan §2b intent-style keys (agentic_retrieval intents)
        "factual_lookup": 0.10,
        "synthesis": 0.30,
        "hypothesis_generation": 0.35,
        "anomaly_detection": 0.20,
        "uncertainty_quantification": 0.25,
        "decision_support": 0.30,
        # Legacy spec query-class keys (answer_runs.query_class enum)
        "factual": 0.10,
        "spatial": 0.15,
        "document": 0.30,
        "computation": 0.10,
        "viz": 0.20,
        "unknown": 0.30,
    }

    # Minimum reranker logit score to retain a chunk after cross-encoder scoring.
    # Cross-encoder outputs raw logits; sigmoid(logit) gives a [0,1] probability.
    #
    # Tuning history (ms-marco-MiniLM-L-6-v2 era — Module 4 Chunk 2 and earlier):
    #   0.0 → recall 1.00, precision 0.420, F1 0.592
    #   0.5 → recall 1.00, precision 0.420, F1 0.592  (inert vs 0.0)
    #   1.0 → recall 1.00, precision 0.420, F1 0.592
    #   2.0 → recall 1.00, precision 0.520, F1 0.684  ← was sweet spot for ms-marco
    #   3.0 → recall 1.00, precision 0.520, F1 0.684  (equivalent)
    #
    # Latency-fix follow-up (2026-05-20) — re-ran the sweep on the CURRENT
    # bge-reranker-base model (Module 4 Chunk 3 swap) and the score scale
    # has shifted. New numbers:
    #   0.0 → recall 1.00, precision 0.10, F1 0.182
    #   0.5 → recall 0.40, precision 0.15, F1 0.218
    #   1.0 → recall 0.00, precision 0.00, F1 0.000
    #   2.0 → recall 0.00, precision 0.00, F1 0.000   ← old default, 100% miss
    #
    # bge-reranker-base concentrates relevant matches in roughly the
    # [-1, +1] logit range vs ms-marco's [0, +4]. The 2.0 cutoff inherited
    # from the old model rejected every match. Dropping to 0.0 restores
    # full recall; precision degrades but the LLM weights citations by
    # score internally and the cascade-fix is to fine-tune the reranker
    # (see [[reranker-v1]]) rather than fight the threshold.
    RERANKER_SCORE_THRESHOLD: float = 0.0

    # -------------------------------------------------------------------------
    # Hallucination prevention layer configuration (Section 04i)
    # -------------------------------------------------------------------------
    # NOTE: `RETRIEVAL_MIN_RELEVANCE` used to live here as a duplicate
    # coarse-retrieval cosine threshold. Removed after confirming it was
    # never referenced anywhere in the codebase — the single knob is
    # RETRIEVAL_QUALITY_THRESHOLD below, which the sweep in
    # scripts/sweep_retrieval_threshold.py operates on.

    # Layer 1: minimum relevance score for retrieved chunks (Qdrant cosine similarity
    # after cross-encoder reranking).  Chunks below this threshold are dropped before
    # being returned to the agent; if ALL chunks are dropped the tool returns empty.
    #
    # R8 (Phase 2): swept 0.25 → 0.70 against the retrieval golden set via
    # scripts/sweep_retrieval_threshold.py. Findings:
    #   - Recall stayed at 1.00 across 0.25–0.60 (reranker dominates the
    #     selection; Qdrant cosine floor is effectively inert at any value
    #     below where real chunks start dropping out).
    #   - Recall fell to 0.60 at 0.70 (2/5 cases lost).
    #   - Bumped 0.30 → 0.50 as a conservative one-step raise: preserves full
    #     recall, gives headroom against future noisier corpora, and matches
    #     the review's original B3 recommendation. 0.60 would be safe today;
    #     leave that step for when the golden set expands.
    RETRIEVAL_QUALITY_THRESHOLD: float = 0.5

    # Layer 3: verify every integer/float in the response text against tool call results.
    # When disabled the validator still logs but does not raise ModelRetry.
    NUMERICAL_VERIFICATION_ENABLED: bool = True

    # Phase H — Layer 3 retry-escalation threshold. Historically Layer 3
    # was log-only ("advisory") even when the model emitted many ungrounded
    # numbers. The new policy:
    #   - >= NUMERIC_RETRY_THRESHOLD ungrounded numbers in one answer
    #     escalates Layer 3 from advisory to HIGH severity → triggers
    #     an LLM retry with a correction hint.
    #   - Any Layer 3 number co-located with a Layer 6 constraint
    #     violation escalates to CRITICAL → also triggers retry.
    # Default 3 — empirically separates "model rounded a citation"
    # (1-2 ungrounded numbers) from "model is fabricating" (3+).
    NUMERIC_RETRY_THRESHOLD: int = 3

    # Layer 4: resolve drill-hole IDs and quoted entity names against PostGIS / Neo4j.
    ENTITY_RESOLUTION_ENABLED: bool = True

    # Layer 6: apply SME-defined geological constraint rules to numerical claims.
    GEOLOGICAL_CONSTRAINTS_ENABLED: bool = True

    # Maximum number of output validation retries before the agent gives up.
    # Pydantic AI's output_retries on the agent is a separate knob for schema
    # validation; this constant governs our domain validators.
    #
    # When LLM_BACKEND=anthropic and adaptive thinking is active, the model
    # self-corrects during generation for most numerical/entity issues, so a
    # single retry is typically enough. Ollama/vLLM benefit from 2. Leave the
    # default at 2 and let operators drop to 1 for the Anthropic deployment.
    MAX_VALIDATION_RETRIES: int = 2

    # Doc-phase 186 — §04i guard tolerance thresholds (Phase E.3.1).
    #
    # The four orchestrator guards (numeric / entity / completeness) are
    # binary pass/fail by default — any single ungrounded number / unresolved
    # entity / uncited sentence transitions the answer to 'rejected' state.
    #
    # On noisier corpora (Phase E.1 OCR'd content, fragmented narrative),
    # legitimate answers get over-refused because the LLM mentions a single
    # number it inferred from a PRE-COMPUTED SUMMARY block, or a transition
    # word ("Additionally") gets misclassified as an entity.
    #
    # These tolerance knobs allow up to N "soft failures" per guard before
    # the bundle marks the answer rejected. Setting any to 0 restores the
    # original strict behavior.
    #
    # Recommended values for OCR-heavy corpora: 2 / 2 / 2.
    # For clean curated corpora: 0 / 0 / 0 (strict).
    GUARD_TOLERANCE_NUMERIC_UNGROUNDED: int = 2
    GUARD_TOLERANCE_ENTITY_UNRESOLVED: int = 2
    GUARD_TOLERANCE_COMPLETENESS_UNCITED: int = 2

    # Eval 01 follow-up — L3 numeric-tuple atomicity guard mode.
    #
    # Phase rollout for the (value, unit) cross-check that catches
    # unit-pair fabrication ("37 oz/t" while evidence carries "37 g/t"):
    #   "shadow" — telemetry only; never affects pass/fail. Default
    #              until ~2 weeks of real-traffic data confirms the
    #              extractor doesn't over-flag.
    #   "warn"   — emits a validation_warning per mismatch so the
    #              orchestrator surfaces it but does NOT reject.
    #   "fail"   — full guard semantics; mismatched tuples reject the
    #              answer (treated identically to ungrounded numbers).
    #
    # Promote with one env var bump after the shadow data lands — no
    # code change required. See `app.agent.hallucination.orchestrator_validators`.
    L3_TUPLE_GUARD_MODE: str = "shadow"

    # -------------------------------------------------------------------------
    # Context budget — how much retrieved data we feed the LLM per request
    # (Section 05, Section 04i Layer 1)
    # -------------------------------------------------------------------------
    #
    # The orchestrator builds a single text context from each tool call's
    # result, capped per category to keep prompt size bounded. Defaults are
    # tuned for qwen2.5:14b (32K native window). With LLM_BACKEND=anthropic
    # on Opus 4.7 (1M ctx) or Sonnet 4.6 (1M ctx), all of these can be raised
    # 5-10× to ground synthesis queries on the full corpus of retrieved data.
    #
    # Override individually in .env. An alternative is to bump the token
    # budget and leave the per-category caps high — the _build_context
    # function will still fit because token truncation is applied last.

    # Overall prompt-side token budget for the CONTEXT block — BACKEND-AWARE.
    # Module 5 Chunk 2 (2026-04-21): model flip to qwen3-14b-awq MoE.
    # OLLAMA_NUM_CTX reduced from 24576 → 8192 (MoE + q8_0 KV cache at 24K
    # context would exceed 16 GB VRAM budget on RTX 4080). Proportional
    # reduction: 24000 * (8192/24576) ≈ 8000; leaving ~700 tokens for system
    # prompt overhead + completion headroom gives 7500 as the safe ceiling.
    # TOOL-CALL-01 fix (2026-04-21): context raised to 16K. 16K context matches
    # Qwen 3 14B capacity + thinking disabled on grounded synthesis; KV cache
    # at 16K fits in ~400 MB within RTX 4080's 800 MB VRAM headroom. With thinking
    # off, the full 4096-token answer budget is available (no thinking consumption).
    # Per TOOL-CALL-01 fix 2026-04-21.
    # Anthropic Claude models (Opus 4.7 / Sonnet 4.5 / Haiku 4.5) ship with
    # 1M-token context — 200K leaves multi-document narrative queries real
    # headroom. The `effective_max_context_tokens` property picks the right
    # value per LLM_BACKEND at read time.
    #
    # Per-query-class budgets (CTX-01 partial resolution):
    # Spatial queries benefit from broader evidence (collar metadata is verbose).
    # Computation queries need less context but more LLM reasoning headroom.
    # These are starter values; tune after Phase C measurement pass.
    #
    # Spatial-candidate preservation rule (CTX-03 partial resolution):
    # When context is truncated to fit within the token budget, spatial tool
    # results (SpatialQueryResult, DownholeLogsResult, AssayDataResult rows)
    # are ALWAYS included first before the budget applies to remaining
    # candidates. This prevents spatial hits from being first-dropped under
    # token pressure purely because they were scored lower than document
    # chunks. Document chunks are trimmed last. The truncation order is:
    #   1. Spatial candidates (always included, up to per-class budget)
    #   2. Remaining candidates sorted by composite key:
    #      primary = reranker_score (if present), secondary = rrf_score,
    #      tertiary = source_store priority (graph > document > public_geo)
    # A4500 profile (2026-05-08 hardware refresh) — see .env.example §
    # "Context budget" for the profile table. The Settings default is the
    # A4500 value so operators that copy .env.example land on a consistent
    # number. Caller MUST stay within the live `VLLM_MAX_MODEL_LEN` of
    # the deployed vLLM container — raising this past the model_len ceiling
    # surfaces as 400-class errors from vLLM, not silent truncation.
    MAX_CONTEXT_TOKENS: int = 22_000              # A4500 / qwen3-14b-awq (16K model_len)
    MAX_CONTEXT_TOKENS_ANTHROPIC: int = 200_000   # Claude 1M ctx, 200K leaves response headroom


    @property
    def effective_max_context_tokens(self) -> int:
        """Return the token budget appropriate for the active LLM_BACKEND.

        Anthropic gets the generous budget (Claude 1M ctx); local
        OpenAI-compatible backends keep the 24K ceiling to match their
        window. Callers that truncate context should use this property
        rather than the raw MAX_CONTEXT_TOKENS setting.
        """
        if self.LLM_BACKEND == "anthropic":
            return self.MAX_CONTEXT_TOKENS_ANTHROPIC
        return self.MAX_CONTEXT_TOKENS

    # Per-category row caps inside _build_context.
    MAX_CONTEXT_COLLARS: int = 20
    MAX_CONTEXT_DOC_CHUNKS: int = 5
    MAX_CONTEXT_GRAPH_ENTITIES: int = 20
    MAX_CONTEXT_PG_RECORDS: int = 12


# Module-level singleton — imported by all other modules as:
#   from app.config import settings
settings = Settings()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Per-query-class token budget table (CTX-01 partial resolution — Chunk 2)
# ---------------------------------------------------------------------------
# Module 5 Chunk 2 (2026-04-21): starter per-class budgets for qwen3-14b-awq
# 8K context window. All values must be ≤ MAX_CONTEXT_TOKENS for
# Ollama/vLLM backends. The Anthropic backend uses MAX_CONTEXT_TOKENS_ANTHROPIC
# for all classes (200K — no truncation concern).
#
# TOOL-CALL-01 fix (2026-04-21): all values scaled 2x to match the raised
# OLLAMA_NUM_CTX=16384 context window. 16K context matches Qwen3-14B
# capacity + thinking disabled on grounded synthesis; KV cache at 16K fits
# in ~400 MB within RTX 4080's 800 MB VRAM headroom. Proportions preserved.
# Per TOOL-CALL-01 fix 2026-04-21.
#
# Usage in the orchestrator's context truncation step:
#   from app.config import settings, MAX_CONTEXT_TOKENS_PER_CLASS
#   per_class_budget = MAX_CONTEXT_TOKENS_PER_CLASS.get(spec_class, settings.MAX_CONTEXT_TOKENS)
#   effective_budget = min(per_class_budget, settings.effective_max_context_tokens)
#
# Tune values after Phase C measurement pass (Module 5 Phase C golden-corpus
# recall/MRR evaluation pairs with Module 10 golden corpus).
MAX_CONTEXT_TOKENS_PER_CLASS: dict[str, int] = {
    "factual":     14_000,  # factual: moderate context, compact answers
    "spatial":     15_000,  # spatial: most verbose (collar metadata + aggregates)
    "document":    15_000,  # document: NI 43-101 chunks are long, need full budget
    "computation": 13_000,  # computation: needs reasoning headroom, less raw context
    "viz":         13_000,  # viz: payload construction, minimal text evidence needed
    "unknown":     14_000,  # unknown: conservative fallback
}
