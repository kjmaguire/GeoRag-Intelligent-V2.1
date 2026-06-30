"""Unit tests for context_enricher service."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ingest.context_enricher import (
    _MAX_ENRICHED_LENGTH,
    _combine_enriched,
    _make_enrichment_prompt,
    enrich_passage_context,
)


def test_make_enrichment_prompt_contains_title():
    prompt = _make_enrichment_prompt(
        document_title="BattleNorth NI 43-101",
        ordinal=3,
        total_passages=50,
        text="Gold grades averaged 2.5 g/t Au.",
    )
    assert "BattleNorth NI 43-101" in prompt
    assert "passage 4 of 50" in prompt
    assert "2.5 g/t Au" in prompt


def test_combine_enriched_format():
    result = _combine_enriched("This passage discusses gold grades.", "Gold grades averaged 2.5 g/t.")
    assert result == "This passage discusses gold grades.\n\nGold grades averaged 2.5 g/t."


def test_combine_enriched_truncation():
    long_text = "x" * 5000
    result = _combine_enriched("Header.", long_text)
    assert len(result) <= _MAX_ENRICHED_LENGTH


def test_context_header_capped_at_300_chars():
    long_header = "H" * 500
    result = _combine_enriched(long_header, "Original text.")
    # The header portion should be capped
    header_part = result.split("\n\n")[0]
    assert len(header_part) <= 300


@pytest.mark.asyncio
async def test_enrich_no_pending_passages():
    """Returns zero enriched when no pending rows exist."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []

    with patch("asyncpg.connect", return_value=mock_conn):
        with patch("app.services.ingest.context_enricher._dsn", return_value="dsn"):
            with patch("app.config.settings") as mock_settings:
                mock_settings.VLLM_URL = "http://vllm:8000/v1"
                mock_settings.VLLM_MODEL = "Qwen/Qwen3-14B-AWQ"
                result = await enrich_passage_context(
                    workspace_id="ws-1", project_id=None
                )

    assert result.passages_seen == 0
    assert result.passages_enriched == 0


@pytest.mark.asyncio
async def test_enrich_writes_combined_content():
    """When LLM returns a header, UPDATE is called with combined text."""
    fake_row = {
        "passage_id": "aaaaaaaa-0000-0000-0000-000000000001",
        "text": "Gold grades here.",
        "ordinal": 0,
        "document_title": "Test Report",
        "total_passages": 10,
    }
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [fake_row]
    mock_conn.execute = AsyncMock()

    mock_http = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "This passage discusses gold grades."}}]
    }
    mock_http.post = AsyncMock(return_value=mock_resp)

    with patch("asyncpg.connect", return_value=mock_conn):
        with patch("app.services.ingest.context_enricher._dsn", return_value="dsn"):
            with patch("app.config.settings") as mock_settings:
                mock_settings.VLLM_URL = "http://vllm:8000/v1"
                mock_settings.VLLM_MODEL = "Qwen/Qwen3-14B-AWQ"
                result = await enrich_passage_context(
                    workspace_id="ws-1",
                    project_id=None,
                    http_client=mock_http,
                )

    assert result.passages_enriched == 1
    assert result.passages_skipped == 0
    # Verify UPDATE was called
    update_calls = [
        c for c in mock_conn.execute.call_args_list
        if "UPDATE" in str(c)
    ]
    assert len(update_calls) == 1
