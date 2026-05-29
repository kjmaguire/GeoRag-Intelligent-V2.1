"""Unit tests for the Anthropic Batch client
(georag_dagster/clients/anthropic_batch.py).

No live HTTP — `AnthropicBatchClient._client` is monkeypatched with a
MagicMock so every HTTP call is fully controllable.

Covers:
- api_key validation (empty → AnthropicBatchError)
- submit: empty requests, duplicate custom_id, happy path, serialization shape
- submit: extended_output beta header on vs off
- get_status parsing
- wait_for_completion: ends immediately, times out, polls multiple times
- iter_results: streaming JSONL parse, succeeded + errored branches
- 5xx retry (2 failures then success)
- 4xx fails fast (no retry)
- close / context manager

Run with:  pytest tests/test_anthropic_batch_client.py -v
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from georag_dagster.clients.anthropic_batch import (
    BETA_HEADER_EXTENDED_OUTPUT,
    AnthropicBatchClient,
    AnthropicBatchError,
    AnthropicBatchTimeout,
    BatchRequest,
    BatchResult,
    BatchStatus,
)


# ─── helpers ──────────────────────────────────────────────────────────────


def _make_response(
    status_code: int = 200,
    json_body: dict[str, Any] | None = None,
    text_body: str = "",
) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.text = text_body or json.dumps(json_body or {})
    resp.json = MagicMock(return_value=json_body or {})
    resp.request = MagicMock()
    resp.raise_for_status = MagicMock()
    return resp


def _client_with_mock_http(
    *,
    extended_output: bool = False,
    responses: list[MagicMock] | None = None,
) -> tuple[AnthropicBatchClient, MagicMock]:
    """Return a client whose underlying httpx.Client is a MagicMock.

    `responses` feeds the `.request()` call in order. If a single response
    should answer every call, supply a list with one entry and use
    `side_effect = itertools.cycle([...])` at the call site.
    """
    client = AnthropicBatchClient(
        api_key="sk-ant-test",
        extended_output=extended_output,
    )
    mock = MagicMock(spec=httpx.Client)
    if responses:
        mock.request.side_effect = responses
    client._client = mock  # type: ignore[assignment]
    return client, mock


# ─── api_key validation ───────────────────────────────────────────────────


def test_empty_api_key_raises() -> None:
    with pytest.raises(AnthropicBatchError, match="requires an api_key"):
        AnthropicBatchClient(api_key="")


# ─── submit ───────────────────────────────────────────────────────────────


def test_submit_empty_requests_raises() -> None:
    client, _ = _client_with_mock_http()
    with pytest.raises(AnthropicBatchError, match="requests list is empty"):
        client.submit([])


def test_submit_duplicate_custom_id_raises() -> None:
    client, _ = _client_with_mock_http()
    reqs = [
        BatchRequest(custom_id="x", messages=[], model="claude-sonnet-4-6"),
        BatchRequest(custom_id="x", messages=[], model="claude-sonnet-4-6"),
    ]
    with pytest.raises(AnthropicBatchError, match="duplicate custom_id"):
        client.submit(reqs)


def test_submit_happy_path_returns_batch_id() -> None:
    resp = _make_response(200, {"id": "batch_abc123"})
    client, mock = _client_with_mock_http(responses=[resp])

    batch_id = client.submit(
        [
            BatchRequest(
                custom_id="rpt-1",
                messages=[{"role": "user", "content": "hello"}],
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system="you are an assistant",
            )
        ]
    )

    assert batch_id == "batch_abc123"
    assert mock.request.call_count == 1

    # Inspect the submitted body — confirms serialization shape.
    _, call_kwargs = mock.request.call_args
    body = call_kwargs["json"]
    assert "requests" in body
    assert len(body["requests"]) == 1
    entry = body["requests"][0]
    assert entry["custom_id"] == "rpt-1"
    params = entry["params"]
    assert params["model"] == "claude-sonnet-4-6"
    assert params["max_tokens"] == 2048
    assert params["messages"] == [{"role": "user", "content": "hello"}]
    assert params["system"] == "you are an assistant"


def test_submit_response_without_id_raises() -> None:
    resp = _make_response(200, {"missing": "id"})
    client, _ = _client_with_mock_http(responses=[resp])
    with pytest.raises(AnthropicBatchError, match="missing `id`"):
        client.submit(
            [BatchRequest(custom_id="a", messages=[], model="claude-sonnet-4-6")]
        )


def test_submit_caller_params_override_defaults() -> None:
    resp = _make_response(200, {"id": "batch_xyz"})
    client, mock = _client_with_mock_http(responses=[resp])
    client.submit(
        [
            BatchRequest(
                custom_id="a",
                messages=[{"role": "user", "content": "q"}],
                model="claude-opus-4-7",
                max_tokens=1024,
                params={"temperature": 0.05, "stop_sequences": ["###"]},
            )
        ]
    )
    body = mock.request.call_args.kwargs["json"]
    params = body["requests"][0]["params"]
    assert params["temperature"] == 0.05
    assert params["stop_sequences"] == ["###"]


# ─── beta header ──────────────────────────────────────────────────────────


def test_extended_output_flag_sets_beta_header() -> None:
    client = AnthropicBatchClient(api_key="sk-ant-x", extended_output=True)
    headers = dict(client._client.headers)
    assert headers.get("anthropic-beta") == BETA_HEADER_EXTENDED_OUTPUT
    client.close()


def test_no_extended_output_omits_beta_header() -> None:
    client = AnthropicBatchClient(api_key="sk-ant-x", extended_output=False)
    headers = dict(client._client.headers)
    assert "anthropic-beta" not in headers
    client.close()


# ─── get_status ───────────────────────────────────────────────────────────


def test_get_status_parses_fields() -> None:
    raw = {
        "id": "batch_abc",
        "processing_status": "in_progress",
        "request_counts": {"processing": 5, "succeeded": 2},
        "created_at": "2026-04-16T12:00:00Z",
        "ended_at": None,
        "results_url": None,
    }
    resp = _make_response(200, raw)
    client, _ = _client_with_mock_http(responses=[resp])

    status = client.get_status("batch_abc")
    assert isinstance(status, BatchStatus)
    assert status.batch_id == "batch_abc"
    assert status.processing_status == "in_progress"
    assert status.request_counts == {"processing": 5, "succeeded": 2}
    assert status.ended_at is None


# ─── wait_for_completion ──────────────────────────────────────────────────


def test_wait_for_completion_returns_immediately_if_ended() -> None:
    resp = _make_response(
        200,
        {"id": "batch_done", "processing_status": "ended", "results_url": "https://x"},
    )
    client, _ = _client_with_mock_http(responses=[resp])
    out = client.wait_for_completion("batch_done", poll_interval=0.01, timeout=5.0)
    assert out.processing_status == "ended"


def test_wait_for_completion_polls_until_end() -> None:
    progressing = _make_response(200, {"id": "b", "processing_status": "in_progress"})
    ended = _make_response(
        200, {"id": "b", "processing_status": "ended", "results_url": "https://r"}
    )
    client, _ = _client_with_mock_http(responses=[progressing, progressing, ended])
    out = client.wait_for_completion("b", poll_interval=0.01, timeout=5.0)
    assert out.processing_status == "ended"


def test_wait_for_completion_raises_timeout() -> None:
    progressing = _make_response(200, {"id": "b", "processing_status": "in_progress"})
    # Return progressing forever.
    client, mock = _client_with_mock_http()
    mock.request.return_value = progressing
    with pytest.raises(AnthropicBatchTimeout):
        client.wait_for_completion("b", poll_interval=0.01, timeout=0.05)


# ─── iter_results ─────────────────────────────────────────────────────────


def test_iter_results_raises_when_not_ended() -> None:
    resp = _make_response(200, {"id": "b", "processing_status": "in_progress"})
    client, _ = _client_with_mock_http(responses=[resp])
    with pytest.raises(AnthropicBatchError, match="not ended"):
        list(client.iter_results("b"))


def test_iter_results_raises_when_no_results_url() -> None:
    resp = _make_response(
        200, {"id": "b", "processing_status": "ended", "results_url": None}
    )
    client, _ = _client_with_mock_http(responses=[resp])
    with pytest.raises(AnthropicBatchError, match="no results_url"):
        list(client.iter_results("b"))


def test_iter_results_streams_and_parses_jsonl() -> None:
    status = _make_response(
        200,
        {
            "id": "b",
            "processing_status": "ended",
            "results_url": "https://example.com/results",
        },
    )

    lines = [
        json.dumps(
            {
                "custom_id": "rpt-1",
                "result": {
                    "type": "succeeded",
                    "message": {"content": [{"type": "text", "text": "hi"}]},
                },
            }
        ),
        json.dumps(
            {
                "custom_id": "rpt-2",
                "result": {
                    "type": "errored",
                    "error": {"type": "overloaded_error", "message": "busy"},
                },
            }
        ),
        "",  # blank line — client should skip
    ]

    stream_resp = MagicMock()
    stream_resp.__enter__ = MagicMock(return_value=stream_resp)
    stream_resp.__exit__ = MagicMock(return_value=None)
    stream_resp.raise_for_status = MagicMock()
    stream_resp.iter_lines = MagicMock(return_value=iter(lines))

    client, mock = _client_with_mock_http(responses=[status])
    mock.stream = MagicMock(return_value=stream_resp)

    results = list(client.iter_results("b"))

    assert len(results) == 2
    assert all(isinstance(r, BatchResult) for r in results)

    ok = results[0]
    assert ok.custom_id == "rpt-1"
    assert ok.result_type == "succeeded"
    assert ok.message == {"content": [{"type": "text", "text": "hi"}]}
    assert ok.error is None

    err = results[1]
    assert err.custom_id == "rpt-2"
    assert err.result_type == "errored"
    assert err.message is None
    assert err.error == {"type": "overloaded_error", "message": "busy"}


def test_iter_results_raises_on_malformed_jsonl() -> None:
    status = _make_response(
        200,
        {
            "id": "b",
            "processing_status": "ended",
            "results_url": "https://example.com/results",
        },
    )
    stream_resp = MagicMock()
    stream_resp.__enter__ = MagicMock(return_value=stream_resp)
    stream_resp.__exit__ = MagicMock(return_value=None)
    stream_resp.raise_for_status = MagicMock()
    stream_resp.iter_lines = MagicMock(return_value=iter(["{not valid json"]))

    client, mock = _client_with_mock_http(responses=[status])
    mock.stream = MagicMock(return_value=stream_resp)

    with pytest.raises(AnthropicBatchError, match="malformed JSONL"):
        list(client.iter_results("b"))


# ─── retry behaviour ──────────────────────────────────────────────────────


def test_5xx_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    # No real sleeping in tests.
    monkeypatch.setattr(
        "georag_dagster.clients.anthropic_batch.time.sleep", lambda *_a, **_kw: None
    )

    fail1 = _make_response(500, text_body="boom")
    fail2 = _make_response(503, text_body="busy")
    ok = _make_response(200, {"id": "batch_ok"})

    client, mock = _client_with_mock_http(responses=[fail1, fail2, ok])
    batch_id = client.submit(
        [BatchRequest(custom_id="a", messages=[], model="claude-sonnet-4-6")]
    )
    assert batch_id == "batch_ok"
    assert mock.request.call_count == 3


def test_4xx_fails_fast_no_retry() -> None:
    fail = _make_response(400, text_body="bad request")
    client, mock = _client_with_mock_http(responses=[fail])
    with pytest.raises(AnthropicBatchError, match="400"):
        client.submit(
            [BatchRequest(custom_id="a", messages=[], model="claude-sonnet-4-6")]
        )
    assert mock.request.call_count == 1


# ─── context manager ──────────────────────────────────────────────────────


def test_context_manager_closes_underlying_client() -> None:
    with AnthropicBatchClient(api_key="sk-ant-x") as client:
        inner = MagicMock(spec=httpx.Client)
        client._client = inner  # type: ignore[assignment]
    inner.close.assert_called_once()
