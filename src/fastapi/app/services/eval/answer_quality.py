"""RAGAS-style answer quality scoring using Qwen3 as judge.

Scores two dimensions per RAG answer:
  faithfulness      — fraction of answer claims supported by retrieved passages
  context_precision — fraction of retrieved passages that were actually useful

Both scored 0.0–1.0 by Qwen3-14B-AWQ via direct vLLM calls.
Failures return 0.0 scores so the system never blocks on scoring failures.

Usage:
    scores = await score_answer_quality(
        question="What gold grades were reported?",
        passages=["Grade averaged 2.5 g/t Au...", "Drilling intersected..."],
        answer="Gold grades averaged 2.5 g/t Au.",
    )
"""
from __future__ import annotations

import json
import logging
import re

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger("georag.eval.answer_quality")

_PASSAGES_CHAR_CAP = 3_000


class AnswerQualityScores(BaseModel):
    faithfulness_score: float = Field(ge=0.0, le=1.0)
    context_precision_score: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""  # brief LLM explanation (not stored to DB)


def _cap_passages(passages: list[str]) -> str:
    joined = "\n---\n".join(passages)
    return joined[:_PASSAGES_CHAR_CAP]


async def _post_json_llm(
    prompt: str,
    http_client: httpx.AsyncClient,
    vllm_url: str,
    vllm_model: str,
) -> dict:
    payload = {
        "model": vllm_model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 200,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    resp = await http_client.post(
        f"{vllm_url}/chat/completions",
        json=payload,
        timeout=30.0,
    )
    resp.raise_for_status()
    raw = resp.json()["choices"][0]["message"]["content"].strip()
    # Strip any <think> blocks that Qwen3 might emit
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    return json.loads(raw)


async def _score_faithfulness(
    question: str,
    passages_text: str,
    answer: str,
    http_client: httpx.AsyncClient,
    vllm_url: str,
    vllm_model: str,
) -> tuple[float, str]:
    prompt = (
        "You are evaluating faithfulness of a RAG system answer. "
        "Faithfulness = fraction of claims in the answer supported by the passages.\n\n"
        f"Question: {question}\n\n"
        f"Retrieved passages:\n{passages_text}\n\n"
        f"Answer to evaluate:\n{answer}\n\n"
        "Score faithfulness 0.0–1.0 where 1.0=every claim is supported, "
        "0.0=answer is fabricated.\n"
        'Respond JSON only: {"faithfulness": <float 0.0-1.0>, "reason": "<1 sentence>"}'
    )
    data = await _post_json_llm(prompt, http_client, vllm_url, vllm_model)
    score = float(data.get("faithfulness", 0.0))
    score = max(0.0, min(1.0, score))
    reason = str(data.get("reason", ""))
    return score, reason


async def _score_context_precision(
    question: str,
    passages: list[str],
    answer: str,
    http_client: httpx.AsyncClient,
    vllm_url: str,
    vllm_model: str,
) -> float:
    numbered = "\n".join(f"{i+1}. {p[:300]}" for i, p in enumerate(passages[:10]))
    prompt = (
        "You are evaluating context precision for a RAG system. "
        "Context precision = fraction of retrieved passages that were useful for answering the question.\n\n"
        f"Question: {question}\n\n"
        f"Retrieved passages:\n{numbered}\n\n"
        f"Answer:\n{answer}\n\n"
        "How many of the passages were actually needed to produce this answer? "
        "Score context precision = useful / total.\n"
        'Respond JSON only: {"context_precision": <float 0.0-1.0>, "useful_count": <int>, "total_count": <int>}'
    )
    data = await _post_json_llm(prompt, http_client, vllm_url, vllm_model)
    score = float(data.get("context_precision", 0.0))
    return max(0.0, min(1.0, score))


async def score_answer_quality(
    *,
    question: str,
    passages: list[str],
    answer: str,
    http_client: httpx.AsyncClient | None = None,
) -> AnswerQualityScores:
    """Score faithfulness and context precision for a RAG answer.

    Returns AnswerQualityScores with scores in [0.0, 1.0].
    Never raises — returns zero scores on any failure.
    """
    from app.config import settings

    if not passages or not answer.strip():
        return AnswerQualityScores(
            faithfulness_score=0.0,
            context_precision_score=0.0,
            reasoning="empty_input",
        )

    own_client = False
    if http_client is None:
        http_client = httpx.AsyncClient()
        own_client = True

    vllm_url = settings.VLLM_URL
    vllm_model = settings.VLLM_MODEL
    passages_text = _cap_passages(passages)

    try:
        faithfulness, reason = await _score_faithfulness(
            question, passages_text, answer, http_client, vllm_url, vllm_model
        )
    except Exception as exc:
        log.warning("answer_quality.faithfulness_failed err=%s", exc)
        faithfulness, reason = 0.0, f"scoring_failed:{type(exc).__name__}"

    try:
        context_precision = await _score_context_precision(
            question, passages, answer, http_client, vllm_url, vllm_model
        )
    except Exception as exc:
        log.warning("answer_quality.context_precision_failed err=%s", exc)
        context_precision = 0.0

    if own_client:
        await http_client.aclose()

    return AnswerQualityScores(
        faithfulness_score=faithfulness,
        context_precision_score=context_precision,
        reasoning=reason,
    )


__all__ = ["score_answer_quality", "AnswerQualityScores"]
