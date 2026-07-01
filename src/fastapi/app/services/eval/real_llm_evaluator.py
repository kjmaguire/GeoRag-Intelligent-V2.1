"""Real vLLM-backed eval evaluator (§10.4) — doc-phase 159.

First real graduation of `evaluate_question` from the synthetic_stub
established in doc-phase 132. Calls vLLM via the OpenAI-compatible
`/v1/chat/completions` endpoint and applies **one real §04i
validator**: refusal correctness (§04i Layer 6 / §2.9).

What's REAL in this graduation:
  - Real LLM call to vLLM at VLLM_URL (qwen3 30B AWQ today)
  - Real response-text capture in actual_payload
  - Real refusal detection via keyword + pattern matching
  - Real grading: passed=True iff the LLM's refused state matches
    question.expected_refusal

What's still synthetic / awaits later graduations:
  - No retrieval (no Qdrant/Neo4j/embedding lookup) — LLM answers
    cold from prompt only. Real RAG evaluator wires AgentDeps + the
    `run_deterministic_rag` orchestrator.
  - No citation/entity/numeric validators — those land per-tick in
    follow-on graduations.

The real evaluator is selected via the `evaluator_kind` parameter
on `run_workspace_evaluation`. Default remains 'synthetic_stub' for
backward compat with doc-phases 132+.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import asyncpg
import httpx

from app.services.eval.workspace_evaluator import (
    QuestionRecord,
    QuestionResult,
)

log = logging.getLogger("georag.eval.real_llm_evaluator")


# Refusal patterns. Real evaluator matches the LLM response against
# these to decide if it refused. Patterns are case-insensitive.
# Doc-phase 163 — patterns + detect_refusal moved to the shared
# `app.services.eval.validators` module so real_llm_v1 and real_rag_v1
# share the same Layer 6 logic. Keep `_REFUSAL_PATTERNS` + `_detect_refusal`
# as backward-compat aliases for existing test imports.
from app.services.eval.validators import (  # noqa: E402
    REFUSAL_PATTERNS as _REFUSAL_PATTERNS,
)
from app.services.eval.validators import (  # noqa: E402
    detect_refusal as _detect_refusal,
)


def _vllm_url() -> str:
    return os.environ.get("VLLM_URL", "http://vllm:8000/v1")


def _vllm_model() -> str:
    """Resolve the model id. Defaults match the docker-compose vLLM service."""
    return os.environ.get("VLLM_MODEL", "Qwen/Qwen3-14B-AWQ")


_SYSTEM_PROMPT = (
    "You are a geological-intelligence assistant for a mineral exploration "
    "company. Answer questions about geology, drilling, and mineral systems "
    "with care for citation provenance. When the question is unanswerable "
    "from the provided context, refuse with a short message starting with "
    "'I cannot' or 'I don't have enough information' — never fabricate."
)


def _detect_refusal(text: str) -> bool:  # noqa: F811
    """Return True if the response text reads as a refusal.

    Conservative heuristic — matches any of the canonical refusal
    phrases. Tighter detection (Layer 6 constraints JSON) lands
    in a future graduation.
    """
    if not text:
        return False
    lower = text.lower()
    return any(p in lower for p in _REFUSAL_PATTERNS)


async def _call_vllm(
    *,
    question_text: str,
    timeout_seconds: float = 30.0,
    max_tokens: int = 256,
) -> tuple[str, int | None]:
    """One OpenAI-compatible call to vLLM. Returns (response_text, total_tokens).

    Raises httpx.HTTPError on network/timeout failures; the caller
    converts these into a `failure_layer='evaluator_not_ready'`
    QuestionResult.
    """
    url = _vllm_url().rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": _vllm_model(),
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": question_text},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        body = r.json()
    text = body["choices"][0]["message"]["content"]
    total_tokens = body.get("usage", {}).get("total_tokens")
    return text, total_tokens


async def evaluate_question_real_llm(
    conn: asyncpg.Connection,
    question: QuestionRecord,
    *,
    timeout_seconds: float = 30.0,
    max_tokens: int = 256,
) -> QuestionResult:
    """Real vLLM-backed evaluation with refusal-correctness check.

    Args:
        conn: asyncpg connection (unused for refusal-only path; real
            RAG evaluator will use it for context_setup application).
        question: golden_questions row.
        timeout_seconds: vLLM call timeout.
        max_tokens: max tokens in the LLM response.

    Returns:
        QuestionResult with:
          - actual_payload carrying the raw response_text + refusal flag
          - passed = (detected_refusal == question.expected_refusal)
          - failure_layer = 'refusal' on mismatch, 'evaluator_not_ready'
            on network/timeout, else None
    """
    t_start = time.monotonic()
    actual_payload: dict[str, Any] = {
        "evaluator": "real_llm_v1",
        "doc_phase": 159,
        "validators_applied": ["refusal_correctness"],
    }

    try:
        response_text, total_tokens = await _call_vllm(
            question_text=question.question_text,
            timeout_seconds=timeout_seconds,
            max_tokens=max_tokens,
        )
    except (httpx.HTTPError, httpx.TimeoutException) as e:
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        log.warning(
            "real_llm_evaluator.vllm_unreachable question_id=%s err=%s",
            question.question_id, e,
        )
        actual_payload["error"] = f"vllm_call_failed: {type(e).__name__}"
        return QuestionResult(
            passed=False,
            actual_payload=actual_payload,
            failure_layer="evaluator_not_ready",
            failure_detail=str(e)[:200],
            latency_ms=elapsed_ms,
            tokens_used=0,
        )

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    detected_refusal = _detect_refusal(response_text)
    refusal_matches_expected = detected_refusal == question.expected_refusal

    actual_payload["response_text"] = response_text[:1000]  # cap for storage
    actual_payload["detected_refusal"] = detected_refusal
    actual_payload["expected_refusal"] = question.expected_refusal
    actual_payload["refusal_matches_expected"] = refusal_matches_expected

    failure_layer: str | None = None
    failure_detail: str | None = None
    if not refusal_matches_expected:
        failure_layer = "refusal"
        failure_detail = (
            f"expected_refusal={question.expected_refusal} but "
            f"detected_refusal={detected_refusal} in response: "
            f"{response_text[:200]!r}"
        )

    log.info(
        "real_llm_evaluator.completed question_id=%s passed=%s "
        "detected_refusal=%s expected=%s tokens=%s latency_ms=%d",
        question.question_id, refusal_matches_expected, detected_refusal,
        question.expected_refusal, total_tokens, elapsed_ms,
    )

    return QuestionResult(
        passed=refusal_matches_expected,
        actual_payload=actual_payload,
        failure_layer=failure_layer,
        failure_detail=failure_detail,
        latency_ms=elapsed_ms,
        tokens_used=total_tokens,
    )


__all__ = [
    "evaluate_question_real_llm",
    "_detect_refusal",
]
