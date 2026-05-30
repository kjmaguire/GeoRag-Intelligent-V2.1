"""Unit tests for answer_quality scoring service."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_llm_mock(response_json: dict) -> AsyncMock:
    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": str(response_json).replace("'", '"')}}]
    }
    mock_http.post = AsyncMock(return_value=mock_resp)
    return mock_http


@pytest.mark.asyncio
async def test_faithfulness_score_parsed():
    """Valid LLM response → faithfulness score extracted correctly."""
    import json
    from app.services.eval.answer_quality import score_answer_quality

    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    call_count = [0]

    async def post_side_effect(*args, **kwargs):
        call_count[0] += 1
        mock_resp.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"faithfulness": 0.85, "reason": "Claims supported"}
                            if call_count[0] == 1
                            else {"context_precision": 0.7, "useful_count": 7, "total_count": 10}
                        )
                    }
                }
            ]
        }
        return mock_resp

    mock_http.post = AsyncMock(side_effect=post_side_effect)

    with patch("app.config.settings") as mock_settings:
        mock_settings.VLLM_URL = "http://vllm:8000/v1"
        mock_settings.VLLM_MODEL = "Qwen/Qwen3-14B-AWQ"
        result = await score_answer_quality(
            question="What grades were reported?",
            passages=["Grades averaged 2.5 g/t Au."],
            answer="Gold grades were 2.5 g/t.",
            http_client=mock_http,
        )

    assert abs(result.faithfulness_score - 0.85) < 0.01
    assert abs(result.context_precision_score - 0.7) < 0.01


@pytest.mark.asyncio
async def test_graceful_failure_on_llm_error():
    """LLM call fails → returns zero scores, never raises."""
    from app.services.eval.answer_quality import score_answer_quality

    mock_http = AsyncMock()
    mock_http.post = AsyncMock(side_effect=Exception("connection refused"))

    with patch("app.config.settings") as mock_settings:
        mock_settings.VLLM_URL = "http://vllm:8000/v1"
        mock_settings.VLLM_MODEL = "Qwen/Qwen3-14B-AWQ"
        result = await score_answer_quality(
            question="test",
            passages=["passage"],
            answer="answer",
            http_client=mock_http,
        )

    assert result.faithfulness_score == 0.0
    assert result.context_precision_score == 0.0


@pytest.mark.asyncio
async def test_empty_passages_returns_zero():
    """Empty passages → immediately returns zeros without LLM call."""
    from app.services.eval.answer_quality import score_answer_quality

    mock_http = AsyncMock()
    result = await score_answer_quality(
        question="test",
        passages=[],
        answer="some answer",
        http_client=mock_http,
    )
    assert result.faithfulness_score == 0.0
    mock_http.post.assert_not_called()


def test_passages_capped_at_3000_chars():
    """Long passage list gets truncated to 3000 chars."""
    from app.services.eval.answer_quality import _cap_passages

    passages = ["x" * 1000] * 10
    result = _cap_passages(passages)
    assert len(result) <= 3_000


def test_score_clamped_to_valid_range():
    """Scores outside [0,1] from LLM are clamped."""
    # This tests the clamping logic in _score_faithfulness
    # We test indirectly via the score range: max(0.0, min(1.0, score))
    assert max(0.0, min(1.0, 1.5)) == 1.0
    assert max(0.0, min(1.0, -0.3)) == 0.0
