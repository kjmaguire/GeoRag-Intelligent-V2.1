"""Phase F.12 — LLM call machinery extracted from ``orchestrator.py``.

This module owns every direct LLM HTTP call site in the deterministic
RAG pipeline:

* :data:`_llm_call_counter` — per-query LLM-call counter (``contextvars``).
* :class:`LLMCallBudgetExceeded` — raised when ``MAX_LLM_CALLS_PER_QUERY``
  is exceeded for the current run.
* :func:`_build_user_message` — assembles the per-turn user role content.
* :func:`_call_openai_compatible_llm` — OpenAI-compatible POST to
  vLLM / Ollama with streaming + Qwen3 sampling discipline.
* :func:`_resolve_local_llm_fallback_target` — picks ``(base_url, model)``
  for the Anthropic→local cross-backend failover path.
* :func:`_call_anthropic_llm` — native Anthropic Messages API with prompt
  caching + adaptive-thinking telemetry.
* :func:`_call_llm` — the backend dispatcher used by the orchestrator's
  retry / failover loop.

``orchestrator.py`` re-exports every symbol below so existing callers
that import ``from app.agent.orchestrator import _call_llm`` keep working
without churn. ``run_deterministic_rag`` itself still owns the retry +
failover loop — only the per-call wire format and budget bookkeeping
moved here.

System-prompt default
---------------------
The two HTTP callers fall back to ``_SYSTEM_PROMPT_DEFAULT`` from
``orchestrator.py`` when the caller doesn't pass one. We import that
constant lazily inside :func:`_default_system_prompt` to avoid a
module-import cycle (``orchestrator`` imports from us; we'd otherwise
import from it at module load). All production call sites pass an
explicit ``system_prompt`` so the lazy path only fires in tests + the
rare back-compat caller.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.agent.query_classification import _sanitize_query
from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# P1 #14 — Global per-query LLM-call cap.
# ---------------------------------------------------------------------------
# A single user query can invoke the LLM multiple times: classifier
# escalation, query rephrasing, primary synthesis, retry-on-validation-fail,
# one-shot failover, follow-ups generation. The contextvar lets us count
# every call without plumbing a counter through every helper signature.
# `run_deterministic_rag` resets the counter at the start of every run.
# `_call_llm` increments and enforces the cap.

_llm_call_counter: contextvars.ContextVar[int] = contextvars.ContextVar(
    "georag_llm_call_counter", default=0
)


# ──────────────────────────────────────────────────────────────────────────
# Per-run cumulative token usage (Eval 09 P3 follow-up).
#
# A query may invoke the LLM multiple times (classifier → rephrase → primary
# synthesis → retry on guard failure → follow-up generation). For the
# answer_runs.input_tokens / output_tokens columns to mean "what this
# query cost," the values must aggregate across all calls in the run.
#
# Producers (`_call_openai_compatible_llm`, `_call_anthropic_llm`) call
# `add_token_usage()` on every response. The orchestrator reads
# `get_run_token_usage()` immediately before the answer_runs INSERT.
# Reset is implicit via `_llm_call_counter` being reset at run start —
# we do the same here for consistency.
# ──────────────────────────────────────────────────────────────────────────

_run_input_tokens: contextvars.ContextVar[int] = contextvars.ContextVar(
    "georag_run_input_tokens", default=0
)
_run_output_tokens: contextvars.ContextVar[int] = contextvars.ContextVar(
    "georag_run_output_tokens", default=0
)


def add_token_usage(input_tokens: int, output_tokens: int) -> None:
    """Accumulate per-run LLM token usage; safe against missing/None values."""
    try:
        if input_tokens:
            _run_input_tokens.set(_run_input_tokens.get() + int(input_tokens))
        if output_tokens:
            _run_output_tokens.set(_run_output_tokens.get() + int(output_tokens))
    except Exception:  # pragma: no cover — never let metrics break the run
        pass


def get_run_token_usage() -> tuple[int, int]:
    """Read (input_tokens, output_tokens) accumulated in this run so far."""
    return _run_input_tokens.get(), _run_output_tokens.get()


def reset_run_token_usage() -> None:
    """Reset both counters; the orchestrator calls this at run start."""
    _run_input_tokens.set(0)
    _run_output_tokens.set(0)


class LLMCallBudgetExceeded(RuntimeError):
    """Raised when a single RAG run exceeds settings.MAX_LLM_CALLS_PER_QUERY.

    The orchestrator catches this near the top-level guard and returns a
    user-facing "request too complex" error rather than letting it
    propagate as a 500. The counter is non-blocking on *informational*
    paths (follow-ups, classifier) — those callers swallow it and skip
    their own work rather than failing the whole query.
    """


class WorkspaceQuotaExceeded(RuntimeError):
    """Raised when the workspace's monthly LLM cost ceiling is exhausted.

    §35.1 hard-stop. The cost_burn_watcher Hatchet workflow sets the
    Redis flag ``workspace:{ws}:llm_suspended`` when accrued spend hits
    the hard_stop_threshold AND admin_override_enabled is false. The
    orchestrator's top-level handler maps this to an HTTP 429 with a
    Retry-After header pointing at the next calendar-month rollover.

    The exception carries the workspace_id for downstream logging /
    metric labelling.
    """

    def __init__(self, workspace_id: str, reason: str = "monthly_cost_limit_exceeded"):
        self.workspace_id = workspace_id
        self.reason = reason
        super().__init__(
            f"workspace {workspace_id} is suspended: {reason}"
        )


async def assert_workspace_not_suspended(
    workspace_id: str | None,
    *,
    redis_client: Any | None = None,
) -> None:
    """Cost-ceiling pre-check fired BEFORE every LLM call.

    Reads the Redis flag ``workspace:{ws}:llm_suspended`` (written by
    cost_burn_watcher). Failure modes:

      - workspace_id is None/empty → no-op (system-level calls have no
        workspace; they're not subject to per-tenant ceilings).
      - Redis unavailable → fail OPEN (allow the call). Cost-burn
        enforcement is a soft contract; an outage of the cost watcher
        infrastructure must not take down the entire chat product.
      - flag present and truthy → raise WorkspaceQuotaExceeded.

    Called from `_call_llm` near the top, before any expensive work.
    """
    if not workspace_id or redis_client is None:
        return
    try:
        flag = await redis_client.get(
            f"workspace:{workspace_id}:llm_suspended"
        )
    except Exception:
        logger.debug(
            "assert_workspace_not_suspended: Redis lookup failed — "
            "failing open. workspace=%s",
            workspace_id, exc_info=True,
        )
        return
    if flag in ("1", b"1", 1, True, "true"):
        raise WorkspaceQuotaExceeded(workspace_id)


def _default_system_prompt() -> str:
    """Lazy-import the runtime default system prompt from orchestrator.

    Avoids a module-import cycle: ``orchestrator`` imports from this
    module at import time; we cannot import the inline
    ``_SYSTEM_PROMPT_DEFAULT`` constant from ``orchestrator`` at the
    top of this file. Resolution is deferred to first call.
    """
    from app.agent.orchestrator import _SYSTEM_PROMPT_DEFAULT  # noqa: PLC0415

    return _SYSTEM_PROMPT_DEFAULT


def _build_user_message(context: str, sanitized_query: str) -> str:
    """Assemble the per-request user message body (CONTEXT + question).

    Kept separate from the static system prompt so the Anthropic path can
    apply cache_control to the system block without invalidating the cache
    every time the context changes.
    """
    return f"CONTEXT:\n{context}\n\nUSER QUESTION: {sanitized_query}\n\nANSWER:"


async def _call_openai_compatible_llm(
    user_message: str,
    temperature: float,
    *,
    system_prompt: str | None = None,
    project_preamble: str | None = None,
    project_facts: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    http_client: Any = None,
    token_callback: Callable[[str], Awaitable[None]] | None = None,
    # Module 5 Chunk 2 — Qwen3 thinking-mode discipline.
    # Default is False: every grounded-synthesis call site explicitly passes
    # enable_thinking=False (TOOL-CALL-01 fix), and structured / JSON paths
    # MUST pass False to avoid <think> leakage into JSON. Setting the
    # default to False makes "no opinion" callers safe-by-default; the few
    # free-text paths that benefit from thinking opt in via the env knob
    # ENABLE_THINKING_FREE_TEXT_DEFAULT (or pass enable_thinking=True at
    # the call site).
    enable_thinking: bool = bool(
        os.getenv("ENABLE_THINKING_FREE_TEXT_DEFAULT", "false").lower() == "true"
    ),
    # Structured-output discipline. When the caller needs JSON (citation-span
    # resolver, tool-call payloads, classifier escalation, atomic-claim
    # extractor, multi-query expansion, etc.), pass `response_format="json"`.
    # Effects on the vLLM path:
    #   1. `response_format: {"type": "json_object"}` is set — vLLM's xgrammar
    #      backend (launch-default since v0.21) constrains decoding to valid
    #      JSON. The model can still emit ANY valid JSON shape.
    #   2. `presence_penalty` switches to QWEN3_PRESENCE_PENALTY_STRUCTURED
    #      (0.0 by default) instead of QWEN3_PRESENCE_PENALTY_NO_THINK (1.5).
    #      The high penalty is correct for free-text (mitigates Qwen3
    #      repetition loops) but hurts JSON — every closing brace and every
    #      repeated field name gets penalised.
    response_format: str | None = None,
    # Schema-constrained decoding. When set, the caller's JSON Schema is
    # forwarded to vLLM as `guided_json` (vLLM's OpenAI extension, served by
    # xgrammar). The engine refuses to emit any token that would invalidate
    # the schema, eliminating the validator-retry loop that
    # `response_format="json"` alone leaves in place. Pair with
    # `response_format="json"` to also get the structured presence_penalty.
    #
    # Wire shape: top-level `guided_json: <schema>` on the request body —
    # vLLM's OpenAI-compat endpoint accepts the field directly (no
    # `extra_body` wrapper needed; that's an OpenAI-SDK convention we bypass
    # by talking to the endpoint over raw httpx).
    #
    # Anthropic path: this parameter is currently ignored (the Anthropic
    # branch enforces JSON via Claude's native prompt-cache + tool-use
    # forcing). Forwarded by the dispatcher for symmetry.
    guided_json: dict[str, Any] | None = None,
) -> str:
    """Call the OpenAI-compatible /v1/chat/completions endpoint (vLLM).

    Historical note: this helper also drove an Ollama backend prior to the
    2026-05-17 cutover. Post-cutover vLLM is the only local-LLM backend;
    the OpenAI-compatible wire shape is preserved so a non-vLLM endpoint
    (LiteLLM proxy, vLLM-equivalent fork) can be substituted at the URL
    layer without code changes.

    Prompt structure (R15):
      - `system` role: static system prompt + per-project preamble. This is
        the stable prefix — identical across requests on the same project
        with the same classifier-selected variant. vLLM's automatic prefix
        cache (engine flag: --enable-prefix-caching) reuses the KV for this
        prefix across requests, so keeping it BYTE-identical by splitting
        it off into a separate message maximises cache locality and lifts
        first-token latency substantially on the second+ query in a session.
      - `user` role: per-turn CONTEXT + USER QUESTION.

    `base_url` and `model` (R12): override the settings-derived target so
    the Anthropic→local-vLLM failover path can hit a specific endpoint
    without mutating LLM_BACKEND globally.

    `http_client` (P1 #13): pooled `httpx.AsyncClient` from app.state.
    When supplied we reuse the connection pool — saves ~30-100 ms TLS
    handshake + warmup per call. When None we fall back to ad-hoc
    construction so tests and pre-pool deploys keep working.

    Streaming
    ---------
    When `token_callback` is supplied we set `stream: true` on the request
    and parse the SSE event stream, forwarding each chunk's `delta.content`
    to the callback. The previous blocking behaviour meant
    time-to-first-token equalled the full generation time on every request;
    real streaming makes FIRST_TOKEN_LATENCY an honest measure of
    user-perceived latency.

    Token cap
    ---------
    `max_tokens = settings.LLM_MAX_OUTPUT_TOKENS` caps generation so a
    model that gets stuck in a repetition loop can't burn the FastAPI 8 s
    deadline. Dynamically reduced when `prompt_tokens + max_tokens` would
    exceed `VLLM_MAX_MODEL_LEN` (see the cap block below).
    """
    static_prompt = system_prompt or _default_system_prompt()
    # Concatenate the three stable blocks in the same order as the
    # Anthropic system_blocks list so vLLM's prefix-cache sees an
    # identical prefix when only the user-message context changes
    # turn-to-turn. The blocks all change at different cadences but
    # putting them in stable order maximises cache hits at every depth.
    system_content_parts: list[str] = [static_prompt]
    if project_preamble:
        system_content_parts.append(project_preamble)
    if project_facts:
        system_content_parts.append(project_facts)
    system_content = "\n\n".join(system_content_parts)
    effective_url = base_url or settings.effective_llm_url
    effective_model = model or settings.effective_llm_model
    stream_enabled = token_callback is not None

    # vLLM is the single local-LLM backend post-2026-05-17. The detection
    # label is preserved as a constant so the metric/log shape doesn't change.
    backend_kind = "vllm"

    # Ollama review #5 — cap output tokens. Match the Anthropic ceiling
    # so the budget is consistent across backends. When thinking is on,
    # bump by LLM_MAX_THINKING_TOKENS so the reasoning trace doesn't eat
    # into the visible-answer budget (Qwen3 reasoning lives in the
    # `reasoning` field but still counts against num_predict on Ollama).
    base_max_output = int(getattr(settings, "LLM_MAX_OUTPUT_TOKENS", 4096))
    thinking_bump = int(getattr(settings, "LLM_MAX_THINKING_TOKENS", 0) or 0)
    max_output = base_max_output + (thinking_bump if enable_thinking else 0)

    # Phase G overnight — dynamic output cap to keep total tokens
    # under VLLM_MAX_MODEL_LEN. Without this, queries with a large
    # context block (project_overview + project preamble + system
    # prompt) push `prompt_tokens + max_tokens` past the model's
    # max_model_len and vLLM returns 400 Bad Request.
    #
    # Tokenisation estimate: Qwen3's tokenizer averages ~2.2-2.8 chars
    # per token on the GeoRAG prompt mix depending on content (system
    # prompt is dense English, JSON-ish context blocks have lots of
    # short tokens, PLSS Township-Range syntax tokenises very finely).
    # Empirical samples seen in production:
    #   Q11  11,362 chars → 4,097 tokens (2.77 chars/token)
    #   Q1   14,151 chars → 5,917 tokens (2.39 chars/token)
    # We use 2.2 chars/token + 512-token safety margin to over-estimate
    # input tokens cleanly even on the finest-grained content. When the
    # prompt itself fills the window we cap output at 64 so vLLM still
    # has SOME room (the 400-BadRequest path then surfaces cleanly to
    # the orchestrator's failover ladder).
    if backend_kind == "vllm":
        _max_model_len = int(getattr(settings, "VLLM_MAX_MODEL_LEN", 8192))
        _prompt_chars = len(system_content) + len(user_message)
        _prompt_tokens_est = int(_prompt_chars / 2.2)
        _safety_margin = 512
        _room_for_output = max(
            64,
            _max_model_len - _prompt_tokens_est - _safety_margin,
        )
        if _room_for_output < max_output:
            logger.info(
                "_call_openai_compatible_llm: capping max_tokens %d -> %d "
                "(prompt_chars=%d prompt_tokens~%d max_model_len=%d)",
                max_output, _room_for_output,
                _prompt_chars, _prompt_tokens_est, _max_model_len,
            )
            max_output = _room_for_output

    # Qwen3 sampling defaults (per Qwen team's published recommendations).
    # vLLM extends the OpenAI-compatible API to accept top_p / top_k / min_p /
    # presence_penalty as top-level fields — we forward them directly.
    qwen3_top_p = float(getattr(settings, "QWEN3_TOP_P", 0.8))
    qwen3_top_k = int(getattr(settings, "QWEN3_TOP_K", 20))
    qwen3_min_p = float(getattr(settings, "QWEN3_MIN_P", 0.0))
    qwen3_no_think_presence = float(
        getattr(settings, "QWEN3_PRESENCE_PENALTY_NO_THINK", 1.5)
    )
    # Separate presence_penalty for JSON / structured paths so the high
    # free-text value (mitigates Qwen3 repetition loops) doesn't break
    # schema compliance — every closing brace and repeated field name
    # would otherwise get penalised.
    qwen3_structured_presence = float(
        getattr(settings, "QWEN3_PRESENCE_PENALTY_STRUCTURED", 0.0)
    )
    structured_output = (response_format or "").lower() == "json"

    request_payload: dict[str, Any] = {
        "model": effective_model,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user",   "content": user_message},
        ],
        "stream": stream_enabled,
        "temperature": temperature,
        "max_tokens": max_output,
    }

    # ── vLLM payload shape ───────────────────────────────────────────────
    # vLLM extends the OpenAI-compatible API with top-level `top_p` /
    # `top_k` / `min_p` / `presence_penalty` fields. Sampling defaults
    # come from the QWEN3_* settings.
    request_payload["top_p"] = qwen3_top_p
    request_payload["top_k"] = qwen3_top_k
    request_payload["min_p"] = qwen3_min_p
    if structured_output:
        request_payload["presence_penalty"] = qwen3_structured_presence
    elif not enable_thinking:
        request_payload["presence_penalty"] = qwen3_no_think_presence

    # vLLM JSON mode uses the OpenAI-compat-standard `response_format` field
    # for "any valid JSON" decoding. For schema-CONSTRAINED decoding the
    # caller passes a `guided_json` dict (see kwarg docstring); we forward
    # it as a top-level field per vLLM's OpenAI-compat extension.
    if structured_output:
        request_payload["response_format"] = {"type": "json_object"}
    if guided_json is not None:
        request_payload["guided_json"] = guided_json

    # Qwen3 chat-template thinking control (Phase 5 follow-up, 2026-05-19).
    # Prior comment claimed vLLM "produces normal output without an explicit
    # reasoning phase" — that assumption broke on vLLM v0.21 + Qwen3-14B-AWQ:
    # the model emits <think>...</think> blocks inline in `content`, which
    # trips the §04i guards (entity/completeness/numeric) on every answer.
    #
    # vLLM v0.21+ forwards `chat_template_kwargs` to the tokenizer's
    # `apply_chat_template()`; Qwen3's chat template reads
    # `enable_thinking` from there. Passing False here suppresses the
    # reasoning emission at the model boundary, which is the load-bearing
    # fix. The defensive strip on the return path (below) handles any
    # residual <think> leakage from future Qwen3 fine-tunes that ignore
    # the flag.
    if not enable_thinking:
        request_payload["chat_template_kwargs"] = {"enable_thinking": False}

    # Backend label for metrics. Same detection as backend_kind above —
    # kept as a separate name so existing log lines / metric labels keep
    # their wire shape ("ollama" / "vllm" strings).
    backend_label = backend_kind

    async def _do_blocking_call(client: httpx.AsyncClient) -> dict:
        response = await client.post(
            f"{effective_url}/chat/completions",
            json=request_payload,
        )
        # Phase G overnight — log the upstream error body BEFORE raising so
        # the orchestrator's retry path has a chance to see *why* (model
        # name typo, context too long, banned token, schema violation).
        # httpx.HTTPStatusError.message is just the URL + status code; the
        # body has the real cause. Guarded with getattr so payload-shape
        # unit tests that stub `httpx.AsyncClient.post` with a
        # `SimpleNamespace` don't trip on missing attributes.
        _status_code = getattr(response, "status_code", None)
        if _status_code is not None and _status_code >= 400:
            try:
                _body = response.text[:500]
            except Exception:
                _body = "<body read failed>"
            logger.error(
                "_call_openai_compatible_llm: %s %d body=%r url=%s model=%s "
                "prompt_chars~%d",
                getattr(getattr(response, "request", None), "method", "POST"),
                _status_code,
                _body,
                effective_url,
                effective_model,
                sum(len(m["content"]) for m in request_payload["messages"]),
            )
        if hasattr(response, "raise_for_status"):
            response.raise_for_status()
        return response.json()

    async def _do_streaming_call(client: httpx.AsyncClient) -> dict:
        """SSE streaming path. Yields each delta to `token_callback`
        and re-assembles the final usage block + completion text."""
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        async with client.stream(
            "POST",
            f"{effective_url}/chat/completions",
            json=request_payload,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                # vLLM emits standard OpenAI SSE: `data: {...}` lines and a
                # final `data: [DONE]` sentinel. The `data: ` prefix strip
                # also covers any non-vLLM OpenAI-compat endpoint that
                # substitutes here.
                if line.startswith("data: "):
                    line = line[len("data: "):]
                if line.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug(
                        "_call_openai_compatible_llm: skipping non-JSON line: %r",
                        line[:120],
                    )
                    continue

                # Choice shape varies: SSE chunks carry `delta.content`,
                # the final blocking-style chunk carries `message.content`.
                choices = chunk.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content is None:
                        content = (choices[0].get("message") or {}).get("content")
                    if content:
                        text_parts.append(content)
                        try:
                            await token_callback(content)
                        except Exception:
                            logger.debug(
                                "token_callback raised; ignoring", exc_info=True
                            )

                # Usage block lives on the final chunk in OpenAI-compat
                # mode — keep the latest non-empty seen so a vLLM build
                # that emits usage progressively still works.
                u = chunk.get("usage")
                if u:
                    usage = u

        return {
            "choices": [{"message": {"content": "".join(text_parts).strip()}}],
            "usage": usage,
        }

    if http_client is not None:
        # Pooled client. Its own timeout is set in lifespan to
        # TIMEOUT_GATHER_S — same as the per-request ad-hoc construction
        # below — so no override needed.
        if stream_enabled:
            data = await _do_streaming_call(http_client)
        else:
            data = await _do_blocking_call(http_client)
    else:
        # Ad-hoc fallback (tests, pre-pool startup). Mirror the split-timeout
        # shape used by the pooled client in lifespan so a vLLM-down condition
        # surfaces fast even on this code path.
        _adhoc_timeout = httpx.Timeout(
            connect=5.0,
            read=settings.TIMEOUT_GATHER_S,
            write=5.0,
            pool=5.0,
        )
        async with httpx.AsyncClient(timeout=_adhoc_timeout) as client:
            if stream_enabled:
                data = await _do_streaming_call(client)
            else:
                data = await _do_blocking_call(client)

    # R15 — log prefix-cache hit rate when the backend reports it.
    # vLLM (and some Ollama builds) expose a `cached_tokens` field on
    # usage. Log via structured fields so Grafana/Loki can count
    # cache-hit rate per (backend, model).
    usage = data.get("usage") or {}
    cached_tokens = int(usage.get("cached_tokens", 0) or 0)
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    add_token_usage(prompt_tokens, completion_tokens)
    if prompt_tokens > 0:
        logger.info(
            "_call_openai_compatible_llm: backend=%s model=%s "
            "prompt_tokens=%d cached_tokens=%d cache_hit_rate=%.2f",
            effective_url,
            effective_model,
            prompt_tokens,
            cached_tokens,
            (cached_tokens / prompt_tokens) if prompt_tokens else 0.0,
        )
        try:
            from app.metrics import PROMPT_CACHE_TOKENS, PROMPT_TOTAL_TOKENS  # noqa: PLC0415
            PROMPT_TOTAL_TOKENS.labels(backend=backend_label).inc(prompt_tokens)
            if cached_tokens > 0:
                PROMPT_CACHE_TOKENS.labels(backend=backend_label).inc(cached_tokens)
        except ImportError:
            pass

    # Empty-content guard (TOOL-CALL-01, 2026-04-21).
    # When the model returns empty `content` but non-empty `reasoning`, the
    # thinking budget was exhausted before any answer text was emitted. Log
    # a structured warning and return a safe fallback so downstream
    # citation/response assembly has something to work with — empty content
    # propagates poorly (triggers sentinel citations, retry loops, etc.).
    # The fallback text is user-visible only when every retry also fails.
    raw_content = data["choices"][0]["message"]["content"]
    content = (raw_content or "").strip()

    # Phase 5 follow-up — defensive <think> strip. The companion fix
    # passes chat_template_kwargs={"enable_thinking": False} which
    # should prevent generation in the first place; this strip catches
    # residual leakage (some Qwen3 fine-tunes/quantizations emit
    # <think> tags even when the template flag is set). Done here so
    # every downstream consumer — the orchestrator's response
    # assembly, §04i guards, Layer 5 provenance, citation lifecycle,
    # the SSE stream to the user — sees a clean answer. The reasoning
    # content remains queryable via the LLM trace if needed.
    if "<think>" in content:
        stripped = re.sub(
            r"<think\b[^>]*>.*?</think>\s*",
            "",
            content,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        if stripped != content:
            logger.debug(
                "_call_openai_compatible_llm: stripped <think> block "
                "(%d -> %d chars)",
                len(content),
                len(stripped),
            )
            content = stripped or content  # never empty the answer
    # `reasoning` is a vendor extension some backends place on
    # message.reasoning (separate from `content`). Not part of the OpenAI
    # spec so we guard carefully.
    reasoning: str = (
        (data.get("choices") or [{}])[0]
        .get("message", {})
        .get("reasoning") or ""
    )
    if not content and reasoning.strip():
        logger.warning(
            "budget_exhausted_by_thinking: empty content with %d-char reasoning. "
            "backend=%s model=%s max_tokens=%d prompt_tokens~%d. "
            "Consider raising VLLM_MAX_MODEL_LEN or disabling thinking on this call site.",
            len(reasoning), backend_label, effective_model, max_output, prompt_tokens,
        )
        # Return a structured fallback so downstream citation/response assembly
        # has something to work with — empty content propagates poorly.
        content = (
            "The model returned no content for this query due to token budget "
            "exhaustion during its internal reasoning pass. This typically happens "
            "on very large projects. Please retry, or raise VLLM_MAX_MODEL_LEN if the "
            "problem persists."
        )

    # Phase 5 follow-up — Langfuse generation observation. Until 2026-05-19
    # the Langfuse SDK was configured but never invoked, so the trace UI
    # was dark. Emitting a generation here gives one record per vLLM call
    # with model + tokens + cache_hit + I/O. Failure-tolerant: if the
    # singleton is unconfigured or unreachable the call no-ops and the
    # request flow is unaffected.
    try:
        from langfuse import get_client  # noqa: PLC0415

        _lf = get_client()
        if _lf is not None and getattr(_lf, "auth_check", None):
            # Modern SDK exposes a singleton via get_client(). Calling
            # auth_check() once at startup would be cleaner, but we keep
            # it lazy here so test envs without Langfuse don't blow up.
            _gen = _lf.start_generation(
                name="vllm_chat_completion",
                model=effective_model,
                input=[
                    {"role": "system", "content": system_content[:4000]},
                    {"role": "user", "content": user_message[:4000]},
                ],
                metadata={
                    "backend": backend_label,
                    "enable_thinking": enable_thinking,
                    "structured_output": structured_output,
                    "temperature": temperature,
                },
            )
            _gen.update(
                output=content[:8000],
                usage_details={
                    "input": prompt_tokens,
                    "output": int(usage.get("completion_tokens", 0) or 0),
                    "cache_read_input_tokens": cached_tokens,
                },
            )
            _gen.end()
    except Exception:
        logger.debug("Langfuse trace emit failed (non-fatal)", exc_info=True)

    return content


def _resolve_local_llm_fallback_target() -> tuple[str, str] | None:
    """R12 — resolve the (base_url, model) for the local-LLM failover.

    Prefers vLLM (the canonical local backend — see docs/model_migration.md)
    when VLLM_URL is set; falls back to LLM_PRIMARY_URL/MODEL otherwise.
    Returns None if neither is configured, which makes the orchestrator
    surface the "LLM error" without trying a cross-backend retry.
    """
    if settings.VLLM_URL:
        return settings.VLLM_URL, settings.VLLM_MODEL
    if settings.LLM_PRIMARY_URL:
        return settings.LLM_PRIMARY_URL, settings.LLM_PRIMARY_MODEL
    return None


async def _call_anthropic_llm(
    user_message: str,
    temperature: float,
    *,
    client: Any = None,
    model: str | None = None,
    system_prompt: str | None = None,
    project_preamble: str | None = None,
    project_facts: str | None = None,
    user_id: str | None = None,
    token_callback: Callable[[str], Awaitable[None]] | None = None,
    previous_answer: str | None = None,
    correction_hint: str | None = None,
    workspace_id: str | None = None,
    pg_pool: Any = None,
) -> str:
    """Call the Anthropic Messages API with prompt caching on the system prompt.

    The static system text is sent as a cacheable block; the per-request
    context + user question is sent as a fresh user message. On cache hit
    this cuts input cost ~90% and first-token latency ~60-80%.

    Priority tier (settings.ANTHROPIC_USE_PRIORITY_TIER) buys guaranteed
    throughput at a cost premium. Leave off for dev; turn on in prod when
    standard-tier 429s start to bite.

    Adaptive thinking is a model-level capability on Opus 4.7 — we do not
    pass an explicit `thinking` parameter, so the model adapts on its own.

    Streaming (P0 #5)
    -----------------
    When ``token_callback`` is supplied we swap from the blocking
    ``messages.create`` path to the streaming ``messages.stream`` path so
    the user sees tokens arrive incrementally. The callback is invoked for
    each text delta; callback exceptions are swallowed (same contract as
    ``status_callback`` — the stream must never break the RAG run).
    Thinking blocks are NOT forwarded through the callback — those remain
    internal and are only logged in aggregate.

    Z.1 / Appendix C §5 — external-LLM egress gate
    ----------------------------------------------
    Before any Anthropic client construction or network egress, we check
    the active workspace's ``profile.allow_external_llm`` policy via
    :func:`app.agent.egress_gate.assert_external_llm_allowed`. When the
    workspace has not opted in (or no workspace context is supplied),
    the gate raises :class:`ExternalLlmEgressBlocked` and the call is
    refused — no prompt content leaves the trust boundary.
    """
    # Z.1 — external-LLM egress profile gate. Fired BEFORE the Anthropic
    # client construction below so a workspace that has not opted in
    # never even instantiates the SDK, let alone sends bytes over TLS.
    from app.agent.egress_gate import assert_external_llm_allowed  # noqa: PLC0415

    await assert_external_llm_allowed(
        workspace_id=workspace_id,
        pg_pool=pg_pool,
    )

    # Prefer a pooled client injected from app.state (B2). If it's missing,
    # hard-fail in prod (R11) — the pooled client should always be present
    # when LLM_BACKEND=anthropic; absence means app.state was never
    # populated, which means every call is paying TLS handshake cost and
    # we want a loud signal, not a silent warning. Tests/migration can set
    # REQUIRE_POOLED_ANTHROPIC_CLIENT=False to allow the old lazy path.
    if client is None:
        if getattr(settings, "REQUIRE_POOLED_ANTHROPIC_CLIENT", True):
            raise RuntimeError(
                "_call_anthropic_llm: pooled AsyncAnthropic client not supplied "
                "(app.state.anthropic_client is None). Check lifespan startup "
                "in src/fastapi/app/main.py. To permit per-call construction "
                "(e.g. in tests), set REQUIRE_POOLED_ANTHROPIC_CLIENT=False."
            )

        from anthropic import AsyncAnthropic  # noqa: PLC0415

        if not settings.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "LLM_BACKEND=anthropic but ANTHROPIC_API_KEY is not set. "
                "Either set the key or change LLM_BACKEND."
            )

        client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        logger.warning(
            "_call_anthropic_llm: pooled client unavailable — built ad-hoc "
            "AsyncAnthropic (REQUIRE_POOLED_ANTHROPIC_CLIENT=False permitted this)"
        )

    # System as a list of content blocks so we can attach cache_control to
    # selected blocks. Everything up to and including a cache_control block
    # is cached (ephemeral TTL: ~5 min for default, ~1 hr for extended).
    #
    # C5: `system_prompt` overrides the default (variants share a preamble
    # so cache locality stays good).
    # C6: `project_preamble` is an OPTIONAL per-project preface carrying
    # stable metadata (name, commodity, CRS, top graph entities). Given its
    # own cache_control block so it's cached per project independently of
    # the shared system prompt.
    static_prompt = system_prompt or _default_system_prompt()
    if settings.ANTHROPIC_ENABLE_PROMPT_CACHING:
        # Cache-control breakpoints (Anthropic limit: 4 ephemeral blocks
        # per message). We use up to 3:
        #   1. static_prompt   — ~rare changes (system-prompt version bump)
        #   2. project_preamble — names: change only when ingestion adds
        #                         entities or operator renames project
        #   3. project_facts    — counts / depth aggregates: change after
        #                         every Dagster materialised-view refresh
        # Putting facts on its own cache_control means a daily ingestion
        # update only invalidates that block; preamble + system stay warm.
        system_blocks: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": static_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if project_preamble:
            system_blocks.append({
                "type": "text",
                "text": project_preamble,
                "cache_control": {"type": "ephemeral"},
            })
        if project_facts:
            system_blocks.append({
                "type": "text",
                "text": project_facts,
                "cache_control": {"type": "ephemeral"},
            })
    else:
        system_blocks = [{"type": "text", "text": static_prompt}]
        if project_preamble:
            system_blocks.append({"type": "text", "text": project_preamble})
        if project_facts:
            system_blocks.append({"type": "text", "text": project_facts})

    extra_headers: dict[str, str] = {}
    if settings.ANTHROPIC_USE_PRIORITY_TIER:
        # Priority tier is opted into per-request via a header. The exact
        # header name is stable across the 4.x generation of models.
        extra_headers["anthropic-priority-tier"] = "priority"

    effective_model = model or settings.effective_llm_model

    # P1 #12 — multi-turn message list when retrying with a correction.
    # The original user turn stays byte-identical so Anthropic's prefix
    # cache hits the cached system + user blocks; only the new
    # assistant + correction turns are uncached. Splicing CORRECTION
    # into the user message instead would invalidate the cache on every
    # retry, doubling token spend on the validation-fail path.
    messages_payload: list[dict[str, Any]] = [
        {"role": "user", "content": user_message}
    ]
    if previous_answer is not None and correction_hint:
        messages_payload.append(
            {"role": "assistant", "content": previous_answer}
        )
        messages_payload.append(
            {
                "role": "user",
                "content": (
                    "Your previous answer had these issues: "
                    f"{correction_hint}. "
                    "Please produce a corrected response now."
                ),
            }
        )

    # P0 #5 — stream text deltas when the caller supplied a token_callback.
    # The Anthropic SDK exposes ``messages.stream`` as an async context
    # manager that yields ``MessageStreamEvent`` objects. We forward each
    # ``text_delta`` to the callback and accumulate the final message via
    # ``get_final_message()`` so all the existing usage / thinking-block
    # telemetry still works unchanged.
    if token_callback is not None:
        async with client.messages.stream(
            model=effective_model,
            max_tokens=settings.ANTHROPIC_MAX_OUTPUT_TOKENS,
            temperature=temperature,
            system=system_blocks,  # type: ignore[arg-type]
            messages=messages_payload,
            extra_headers=extra_headers or None,
            timeout=settings.TIMEOUT_GATHER_S,
        ) as stream:
            async for chunk in stream.text_stream:
                if not chunk:
                    continue
                try:
                    await token_callback(chunk)
                except Exception:
                    # token_callback is a UX affordance, not a correctness
                    # boundary — never fail the LLM call because the consumer
                    # closed its queue.
                    logger.debug("token_callback raised; ignoring", exc_info=True)
            msg = await stream.get_final_message()
    else:
        msg = await client.messages.create(
            model=effective_model,
            max_tokens=settings.ANTHROPIC_MAX_OUTPUT_TOKENS,
            temperature=temperature,
            system=system_blocks,  # type: ignore[arg-type]
            messages=messages_payload,
            extra_headers=extra_headers or None,
            timeout=settings.TIMEOUT_GATHER_S,
        )

    # Log cache metrics when available — these prove the cache is working.
    usage = getattr(msg, "usage", None)
    if usage is not None:
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        _anthropic_output = getattr(usage, "output_tokens", 0) or 0
        # Anthropic returns the uncached portion in `input_tokens`. For the
        # per-run total we want every billable input token (including cache
        # reads + writes), matching how the cost calc downstream treats them.
        add_token_usage(input_tokens + cache_read + cache_write, _anthropic_output)
        logger.info(
            "_call_anthropic_llm: model=%s input=%d output=%d cache_read=%d cache_write=%d",
            effective_model,
            input_tokens,
            getattr(usage, "output_tokens", 0),
            cache_read,
            cache_write,
        )
        # Signal-harvesting metrics (#1 dashboard): track cache hit volume.
        try:
            from app.metrics import (  # noqa: PLC0415
                LLM_COST_USD,
                LLM_TOKENS_OUTPUT,
                PROMPT_CACHE_TOKENS,
                PROMPT_TOTAL_TOKENS,
            )
            from app.agent.pricing import estimate_cost_usd, user_bucket  # noqa: PLC0415

            total_input = input_tokens + cache_read + cache_write
            if total_input > 0:
                PROMPT_TOTAL_TOKENS.labels(backend="anthropic").inc(total_input)
                if cache_read > 0:
                    PROMPT_CACHE_TOKENS.labels(backend="anthropic").inc(cache_read)

            # Cost accountability (→ A grade): every call reports USD cost
            # against (model, user_bucket). Dashboard panels read from
            # rate(georag_llm_cost_usd_total[5m]) to show $/minute and
            # identify heavy-bucket spenders.
            output_tokens = getattr(usage, "output_tokens", 0) or 0
            # P1 #30 — bill cache WRITES too. Anthropic charges 1.25× the
            # input rate for cache_creation_input_tokens (the first call
            # that populates a cache block). Previously these were
            # silently dropped from the cost calc, understating monthly
            # spend by 10-30 % depending on cache rotation frequency.
            cost_usd = estimate_cost_usd(
                model=effective_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cache_read,
                cache_creation_tokens=cache_write,
            )
            if cost_usd > 0:
                # user_bucket is deterministic-hashed from user_id threaded
                # down from the orchestrator (deps.user_id, populated by the
                # Laravel-minted JWT per B7). Falls back to "unknown" on
                # legacy X-Service-Key-only calls.
                LLM_COST_USD.labels(
                    model=effective_model,
                    user_bucket=user_bucket(user_id),
                ).inc(cost_usd)
            if output_tokens > 0:
                LLM_TOKENS_OUTPUT.labels(model=effective_model).inc(output_tokens)
        except ImportError:
            pass

    # Walk the content blocks. Opus 4.7 may interleave `thinking` blocks
    # (adaptive thinking — model-initiated) and `text` blocks. The user
    # response is the concatenation of text blocks; thinking is internal
    # reasoning and is logged for audit / hallucination-prevention review
    # but NEVER forwarded to the user or the downstream assembler. This
    # keeps the "citations mandatory" contract intact — thinking blocks
    # are not evidence and must not appear in answer_runs.
    text_parts: list[str] = []
    thinking_chars = 0
    thinking_blocks = 0
    for block in msg.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text_parts.append(getattr(block, "text", ""))
        elif block_type in ("thinking", "redacted_thinking"):
            thinking_blocks += 1
            thinking_chars += len(getattr(block, "thinking", "") or "")

    if thinking_blocks:
        # Structured event so Grafana/Loki can count adaptive-thinking
        # activation rate by query type. The thinking text itself is
        # intentionally NOT logged — it may contain rephrased user PII
        # and is not needed for the operational metric. If full capture
        # is required for milestone-gate reviews, a separate audit path
        # should write to an access-controlled store.
        logger.info(
            "_call_anthropic_llm: adaptive_thinking model=%s blocks=%d chars=%d",
            effective_model,
            thinking_blocks,
            thinking_chars,
        )

    return "".join(text_parts).strip()


async def _call_llm(
    query: str,
    context: str,
    temperature: float = 0.1,
    *,
    anthropic_client: Any = None,
    openai_http_client: Any = None,
    model: str | None = None,
    system_prompt: str | None = None,
    project_preamble: str | None = None,
    project_facts: str | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
    pg_pool: Any = None,
    token_callback: Callable[[str], Awaitable[None]] | None = None,
    previous_answer: str | None = None,
    correction_hint: str | None = None,
    audit_label: str = "primary",
    # TOOL-CALL-01 fix 2026-04-21: grounded synthesis passes False to avoid
    # thinking tokens consuming the answer budget. Default is False — see
    # the matching parameter on `_call_openai_compatible_llm` for rationale.
    # Free-text callers can flip via ENABLE_THINKING_FREE_TEXT_DEFAULT=true.
    enable_thinking: bool = bool(
        os.getenv("ENABLE_THINKING_FREE_TEXT_DEFAULT", "false").lower() == "true"
    ),
    # MoE review (2026-05-08): when callers need JSON-shaped output, pass
    # response_format="json". Forwarded to the OpenAI-compatible path so
    # `response_format: {"type": "json_object"}` + structured presence_penalty
    # are applied. The Anthropic path uses its own response_format mechanism
    # (tools/JSON mode) and ignores this flag.
    response_format: str | None = None,
    # Schema-constrained decoding on vLLM. When set, vLLM's xgrammar
    # backend refuses to emit tokens that would violate the schema —
    # eliminates the validator-retry loop that response_format="json" alone
    # leaves in place. Forwarded only to the OpenAI-compat path; the
    # Anthropic branch ignores it.
    guided_json: dict[str, Any] | None = None,
) -> str:
    """Make a single LLM call to summarize the context into a plain English answer.

    No tool-calling, no structured output — just straightforward text generation.
    Temperature is query-adaptive: 0.05 for numerical, 0.3 for narrative.

    Dispatches to the OpenAI-compatible path (Ollama/vLLM) or the native
    Anthropic path based on settings.LLM_BACKEND. The Anthropic path enables
    prompt caching on the static system prompt, which typically cuts input
    cost ~90% and first-token latency ~60-80% on cache hits.

    `anthropic_client` and `model` (B1 + B2): the orchestrator passes a
    pooled AsyncAnthropic client and a tier-selected model. When omitted the
    Anthropic path falls back to lazy construction + settings.ANTHROPIC_MODEL.

    `system_prompt` (C5): caller-selected variant (NUMERIC/NARRATIVE/DEFAULT).
    `project_preamble` (C6): stable per-project metadata cached independently.

    P1 #12 — `previous_answer` + `correction_hint`: when both are set,
    the Anthropic path emits a 3-turn conversation:
      [user(question), assistant(previous_answer), user(correction)]
    instead of splicing "CORRECTION:" into the user message. This keeps
    the original user turn byte-identical across retries so Anthropic's
    prompt cache hits the prefix unchanged — only the new correction turn
    is uncached. Splicing into the user message invalidated the cache on
    every retry, doubling token spend on the validation-fail path.

    P1 #14 — `audit_label` is a short tag ("primary", "retry", "failover",
    "follow_ups", "classifier") used for structured per-attempt logging
    so operators can see which code path is driving call volume. The
    contextvar-tracked counter increments on every call and aborts the
    run when it crosses MAX_LLM_CALLS_PER_QUERY.
    """
    # P1 #14 — global cap check.
    n_so_far = _llm_call_counter.get()
    cap = int(getattr(settings, "MAX_LLM_CALLS_PER_QUERY", 8))
    if n_so_far >= cap:
        try:
            from app.metrics import LLM_CALL_BUDGET_EXCEEDED  # noqa: PLC0415
            LLM_CALL_BUDGET_EXCEEDED.inc()
        except ImportError:
            pass
        raise LLMCallBudgetExceeded(
            f"_call_llm: budget of {cap} LLM calls exceeded for this run "
            f"(label={audit_label}, model={model or 'default'})"
        )
    _llm_call_counter.set(n_so_far + 1)
    logger.info(
        "_call_llm: attempt=%d/%d label=%s model=%s temperature=%.2f",
        n_so_far + 1, cap, audit_label, model or "default", temperature,
    )

    # Sanitize user query — strip any prompt injection attempts.
    sanitized_query = _sanitize_query(query)
    user_message = _build_user_message(context, sanitized_query)

    if settings.LLM_BACKEND == "anthropic":
        return await _call_anthropic_llm(
            user_message,
            temperature,
            client=anthropic_client,
            model=model,
            system_prompt=system_prompt,
            project_preamble=project_preamble,
            project_facts=project_facts,
            user_id=user_id,
            token_callback=token_callback,
            previous_answer=previous_answer,
            correction_hint=correction_hint,
            workspace_id=workspace_id,
            pg_pool=pg_pool,
        )
    # OpenAI-compatible (vLLM) path. token_callback is honoured —
    # `_call_openai_compatible_llm` flips `stream: true` and forwards each
    # SSE delta through the callback so first-token latency is the genuine
    # measure.
    # P1 #12: the vLLM path does not benefit from the multi-turn cache
    # trick (vLLM's prefix cache is request-by-request, not multi-turn);
    # we splice CORRECTION inline as a fallback so retries still convey
    # the validation feedback to the model.
    if correction_hint:
        user_message = (
            f"{user_message}\n\n"
            f"CORRECTION: Your previous answer had issues: {correction_hint}. "
            f"Please fix these in your response."
        )
    return await _call_openai_compatible_llm(
        user_message,
        temperature,
        system_prompt=system_prompt,
        project_preamble=project_preamble,
        project_facts=project_facts,
        http_client=openai_http_client,
        token_callback=token_callback,
        enable_thinking=enable_thinking,
        response_format=response_format,
        guided_json=guided_json,
    )


__all__ = [
    "_llm_call_counter",
    "LLMCallBudgetExceeded",
    "_build_user_message",
    "_call_openai_compatible_llm",
    "_resolve_local_llm_fallback_target",
    "_call_anthropic_llm",
    "_call_llm",
]
