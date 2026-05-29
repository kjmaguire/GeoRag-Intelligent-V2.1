"""Anthropic Message Batches client for Dagster LLM-enrichment assets.

The Message Batches API submits up to 100k requests in a single job, runs
them asynchronously (usually within an hour), and returns results in a
newline-delimited JSON file. Pricing is ~50% of the synchronous Messages
API, and the `output-300k-2026-03-24` beta header lifts the per-response
output cap from 128k to 300k tokens — the combination makes it the right
shape for bulk geological-report enrichment:

    - Per-report entity extraction (populate Neo4j `Report` nodes + edges)
    - Per-chunk enrichment for Qdrant (deposit-type tags, stratigraphic
      context) — currently empty because extraction is still deterministic
    - Bulk geological-constraint auditing across the historical corpus

As of this writing Dagster assets do not yet submit any LLM work — this
client is scaffolding for the M2 ingestion work. The API shape is stable
per Anthropic docs; when an asset wires this up, the path is:

    client = AnthropicBatchClient(api_key=settings.ANTHROPIC_API_KEY)
    requests = [
        BatchRequest(custom_id=f"report-{r.id}", messages=[...], system=...)
        for r in reports
    ]
    batch_id = client.submit(requests, model="claude-sonnet-4-6")
    client.wait_for_completion(batch_id)
    for result in client.iter_results(batch_id):
        ...

Design:

  - Synchronous (`httpx.Client`). Dagster asset execution is sync; parallel
    fan-out is across ASSETS, not inside one.
  - Minimal state — nothing is persisted here. If Dagster crashes between
    submit and fetch, the `batch_id` is the only recovery handle needed
    (Anthropic holds results for 29 days). Callers should stash batch_id
    in a Dagster output or a sentinel file.
  - No retry on 4xx. Transient 5xx and connection errors retry with
    exponential backoff. Matches the arcgis_rest.py client's error model.
  - Prompt caching (`cache_control`) is attached at the request-level by
    the caller. This client does not inject caching — that's a per-asset
    decision depending on whether the system prompt is shared.

References:
  - Anthropic Message Batches: docs.anthropic.com/en/docs/build-with-claude/batch-processing
  - Extended output beta: output-300k-2026-03-24 header (Opus 4.7 / Sonnet 4.6)
  - Section 08 (LLM Architecture) in georag-architecture.html
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_INTERVAL_SECONDS = 30.0
DEFAULT_POLL_TIMEOUT_SECONDS = 3600.0  # 1 hour — matches typical batch SLA
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 2.0

# Beta header flag for extended output (300k tokens). Only valid on
# Opus 4.7, Opus 4.6, and Sonnet 4.6 per the Anthropic docs. Older models
# silently ignore the header.
BETA_HEADER_EXTENDED_OUTPUT = "output-300k-2026-03-24"


class AnthropicBatchError(RuntimeError):
    """Raised when a batch submit/poll/fetch fails in a non-retriable way."""


class AnthropicBatchTimeout(RuntimeError):
    """Raised when `wait_for_completion` hits its deadline before the batch
    reached `ended`. The batch is still running on Anthropic's side —
    callers can retry `wait_for_completion(batch_id)` with a longer
    deadline, or come back later with `fetch_results(batch_id)`."""


@dataclass
class BatchRequest:
    """One entry in a batch submission.

    `custom_id` is an opaque caller-chosen identifier that Anthropic echoes
    back verbatim in the results. Keep it stable across retries so the
    caller can correlate by primary key (e.g. `report-{uuid}`).

    `messages` and `system` follow the Messages API shape exactly; this
    client does not validate their structure. `params` is merged into the
    request body so callers can set `temperature`, `stop_sequences`, etc.
    without the client needing to know about each knob.
    """

    custom_id: str
    messages: list[dict[str, Any]]
    model: str
    max_tokens: int = 4096
    system: str | list[dict[str, Any]] | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class BatchResult:
    """One result from a completed batch.

    `result_type` is "succeeded", "errored", "canceled", or "expired" —
    matches the Anthropic response schema. On "succeeded", `message`
    contains the full Messages API response payload; on the other three,
    `error` contains the error detail and `message` is None.
    """

    custom_id: str
    result_type: str
    message: dict[str, Any] | None
    error: dict[str, Any] | None


@dataclass
class BatchStatus:
    """Snapshot of a batch's lifecycle state."""

    batch_id: str
    processing_status: str  # "in_progress" | "ended" | "canceling"
    request_counts: dict[str, int]
    created_at: str | None
    ended_at: str | None
    results_url: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


class AnthropicBatchClient:
    """Thin client around the Anthropic Message Batches API.

    Not thread-safe (one `httpx.Client` per instance). Instantiate inside
    the Dagster asset and let Python GC it on exit.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        anthropic_version: str = DEFAULT_ANTHROPIC_VERSION,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        extended_output: bool = False,
    ) -> None:
        if not api_key:
            raise AnthropicBatchError(
                "AnthropicBatchClient requires an api_key. "
                "Pass settings.ANTHROPIC_API_KEY or the raw key."
            )
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._extended_output = extended_output

        headers = {
            "x-api-key": api_key,
            "anthropic-version": anthropic_version,
            "content-type": "application/json",
        }
        if extended_output:
            # Multiple beta flags are comma-separated per Anthropic's spec.
            # Caller can add more later by setting client.add_beta_header().
            headers["anthropic-beta"] = BETA_HEADER_EXTENDED_OUTPUT

        self._client = httpx.Client(headers=headers, timeout=timeout)

    # ─── public API ───────────────────────────────────────────────────────

    def submit(self, requests: list[BatchRequest]) -> str:
        """Submit a batch. Returns the `batch_id` used by every subsequent call.

        The batch shape is:
            {
              "requests": [
                {
                  "custom_id": "report-abc",
                  "params": {
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 4096,
                    "messages": [...],
                    "system": ...,
                    ...rest of params
                  }
                }, ...
              ]
            }
        """
        if not requests:
            raise AnthropicBatchError("submit: requests list is empty")

        # De-dup custom_ids eagerly — Anthropic rejects duplicates and the
        # error arrives at submit time, which is inconvenient to debug
        # inside Dagster asset retries.
        seen: set[str] = set()
        for req in requests:
            if req.custom_id in seen:
                raise AnthropicBatchError(
                    f"submit: duplicate custom_id '{req.custom_id}' "
                    "(Anthropic rejects the batch at submit time)"
                )
            seen.add(req.custom_id)

        body = {"requests": [self._serialize_request(r) for r in requests]}
        data = self._post_json("/v1/messages/batches", body)
        batch_id = data.get("id")
        if not batch_id:
            raise AnthropicBatchError(
                f"submit: response missing `id`. Raw response: {data!r}"
            )
        return str(batch_id)

    def get_status(self, batch_id: str) -> BatchStatus:
        """Fetch the current processing status of a batch."""
        data = self._get_json(f"/v1/messages/batches/{batch_id}")
        return BatchStatus(
            batch_id=str(data.get("id", batch_id)),
            processing_status=str(data.get("processing_status", "unknown")),
            request_counts=dict(data.get("request_counts", {}) or {}),
            created_at=data.get("created_at"),
            ended_at=data.get("ended_at"),
            results_url=data.get("results_url"),
            raw=data,
        )

    def wait_for_completion(
        self,
        batch_id: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
        timeout: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    ) -> BatchStatus:
        """Poll `get_status` until the batch ends or the deadline hits.

        Polls at `poll_interval` seconds. Exits when `processing_status`
        transitions to "ended". Raises `AnthropicBatchTimeout` if the
        deadline expires — the batch continues running on Anthropic's
        side, so the caller can re-enter with a longer deadline.
        """
        deadline = time.monotonic() + timeout
        last: BatchStatus | None = None
        while time.monotonic() < deadline:
            last = self.get_status(batch_id)
            if last.processing_status == "ended":
                return last
            time.sleep(poll_interval)

        raise AnthropicBatchTimeout(
            f"wait_for_completion: batch {batch_id} still "
            f"{last.processing_status if last else 'unknown'} after {timeout}s"
        )

    def iter_results(self, batch_id: str) -> Iterator[BatchResult]:
        """Stream parsed results from a completed batch.

        Fetches the results file (newline-delimited JSON) and yields one
        `BatchResult` per line. Must only be called after the batch ended —
        otherwise the results URL is not yet populated.
        """
        status = self.get_status(batch_id)
        if status.processing_status != "ended":
            raise AnthropicBatchError(
                f"iter_results: batch {batch_id} is {status.processing_status}, "
                "not ended. Call wait_for_completion first."
            )
        if not status.results_url:
            raise AnthropicBatchError(
                f"iter_results: batch {batch_id} ended but no results_url "
                "was provided. Anthropic may have expired the results "
                "(29-day retention)."
            )

        # Results URL is a pre-signed download. Auth headers go on it
        # because Anthropic requires them even for the results URL.
        with self._client.stream("GET", status.results_url) as resp:
            resp.raise_for_status()
            for raw_line in resp.iter_lines():
                if not raw_line:
                    continue
                try:
                    parsed = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    raise AnthropicBatchError(
                        f"iter_results: malformed JSONL line: {exc}"
                    ) from exc
                yield self._parse_result_line(parsed)

    def close(self) -> None:
        """Close the underlying HTTP client. Safe to call multiple times."""
        try:
            self._client.close()
        except Exception:
            pass

    def __enter__(self) -> AnthropicBatchClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ─── internals ────────────────────────────────────────────────────────

    @staticmethod
    def _serialize_request(req: BatchRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_tokens,
            "messages": req.messages,
        }
        if req.system is not None:
            params["system"] = req.system
        # Caller-supplied params win — lets the caller set temperature,
        # thinking config, or any other Messages API knob without this
        # client needing to know about it.
        params.update(req.params)
        return {"custom_id": req.custom_id, "params": params}

    @staticmethod
    def _parse_result_line(raw: dict[str, Any]) -> BatchResult:
        custom_id = str(raw.get("custom_id", ""))
        result = raw.get("result") or {}
        result_type = str(result.get("type", "unknown"))
        message = result.get("message") if result_type == "succeeded" else None
        error = result.get("error") if result_type != "succeeded" else None
        return BatchResult(
            custom_id=custom_id,
            result_type=result_type,
            message=message,
            error=error,
        )

    def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", path, json=body)

    def _get_json(self, path: str) -> dict[str, Any]:
        return self._request_json("GET", path)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(DEFAULT_RETRIES):
            try:
                resp = self._client.request(method, url, json=json)
            except httpx.HTTPError as exc:
                last_exc = exc
                time.sleep(DEFAULT_BACKOFF_SECONDS * (2**attempt))
                continue

            if resp.status_code >= 500:
                last_exc = httpx.HTTPStatusError(
                    f"server error {resp.status_code}: {resp.text[:400]}",
                    request=resp.request,
                    response=resp,
                )
                time.sleep(DEFAULT_BACKOFF_SECONDS * (2**attempt))
                continue

            if resp.status_code >= 400:
                # 4xx is a caller bug (bad request, auth, rate-limit).
                # Fail loud — retries won't help and they mask the real
                # problem for debugging.
                raise AnthropicBatchError(
                    f"{method} {path} -> {resp.status_code}: {resp.text[:800]}"
                )

            try:
                return resp.json()  # type: ignore[no-any-return]
            except json.JSONDecodeError as exc:
                raise AnthropicBatchError(
                    f"{method} {path} returned non-JSON body: {exc}"
                ) from exc

        raise AnthropicBatchError(
            f"{method} {path} failed after {DEFAULT_RETRIES} attempts: {last_exc}"
        )
