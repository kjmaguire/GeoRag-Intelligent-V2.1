"""Thin synchronous helper around the OpenAI SDK targeted at vLLM.

The reranker-label generation asset needs to fan out ~50k JSON-mode
completions against the in-cluster vLLM server. vLLM speaks the OpenAI
chat-completions wire format, so we can use ``openai.OpenAI`` pointed at
``http://vllm:8000/v1`` directly. The wrapper here adds three things on
top of the raw SDK call:

  - bounded thread-pool fan-out (default 32 concurrent requests)
  - simple exponential-backoff retry on transient 5xx / connection errors
  - explicit ``json.loads`` of the model output with a typed error so the
    caller can decide whether to drop the row or retry

The SDK *requires* a non-empty ``api_key`` even when the upstream server
ignores it (vLLM has no ``--api-key`` flag), so call sites pass
``api_key="EMPTY"``. This file is intentionally small and synchronous —
Dagster assets execute in a sync worker, and fan-out happens with a
``ThreadPoolExecutor`` rather than asyncio.

NOTE: Do NOT add ``from __future__ import annotations`` here — keeping
parity with the other Dagster modules that interact with Pydantic Config
classes.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class VllmJsonParseError(ValueError):
    """Raised when the vLLM response body is not parseable JSON.

    The caller catches this to drop the row (and log the offending text)
    rather than aborting the whole asset materialisation.
    """

    def __init__(self, message: str, raw_text: str) -> None:
        super().__init__(message)
        self.raw_text = raw_text


# ---------------------------------------------------------------------------
# Request / response dataclasses
# ---------------------------------------------------------------------------

@dataclass
class VllmJsonRequest:
    """One JSON-mode chat completion request.

    ``custom_id`` is opaque to vLLM but lets the caller match a result back
    to whichever chunk/query it belongs to once results come back out of
    order from the thread pool.
    """

    custom_id: str
    system: str
    user: str
    max_tokens: int = 200
    temperature: float = 0.2


@dataclass
class VllmJsonResult:
    custom_id: str
    parsed: dict | None
    error: str | None = None
    raw_text: str | None = None
    latency_ms: int | None = None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class VllmOpenAIClient:
    """Synchronous fan-out helper for vLLM's OpenAI-compatible endpoint."""

    def __init__(
        self,
        client: Any,
        model: str,
        max_workers: int = 32,
        max_retries: int = 3,
        retry_backoff_s: float = 1.0,
    ) -> None:
        self._client = client
        self._model = model
        self._max_workers = max_workers
        self._max_retries = max_retries
        self._retry_backoff_s = retry_backoff_s

    # ------------------------------------------------------------------
    # Single-request path (also used internally by run_many)
    # ------------------------------------------------------------------

    def run_one(self, request: VllmJsonRequest) -> VllmJsonResult:
        """Run a single JSON-mode completion. Always returns a result; never raises."""
        started = time.monotonic()
        last_exc: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                resp = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": request.system},
                        {"role": "user", "content": request.user},
                    ],
                    max_tokens=request.max_tokens,
                    temperature=request.temperature,
                    response_format={"type": "json_object"},
                )
                raw_text = resp.choices[0].message.content or ""
                latency_ms = int((time.monotonic() - started) * 1000)
                try:
                    parsed = json.loads(raw_text)
                except json.JSONDecodeError as parse_exc:
                    return VllmJsonResult(
                        custom_id=request.custom_id,
                        parsed=None,
                        error=f"json_parse_error: {parse_exc}",
                        raw_text=raw_text,
                        latency_ms=latency_ms,
                    )
                return VllmJsonResult(
                    custom_id=request.custom_id,
                    parsed=parsed,
                    raw_text=raw_text,
                    latency_ms=latency_ms,
                )
            except Exception as exc:  # noqa: BLE001 — retry on any transport-level failure
                last_exc = exc
                if attempt + 1 < self._max_retries:
                    sleep_s = self._retry_backoff_s * (2 ** attempt)
                    logger.warning(
                        "vllm.run_one custom_id=%s attempt=%d failed (%s); retrying in %.1fs",
                        request.custom_id, attempt + 1, exc, sleep_s,
                    )
                    time.sleep(sleep_s)

        latency_ms = int((time.monotonic() - started) * 1000)
        return VllmJsonResult(
            custom_id=request.custom_id,
            parsed=None,
            error=f"transport_error: {last_exc}",
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------
    # Fan-out
    # ------------------------------------------------------------------

    def run_many(
        self,
        requests: Iterable[VllmJsonRequest],
        progress: Callable[[int, int], None] | None = None,
    ) -> list[VllmJsonResult]:
        """Run requests concurrently. Results returned in arbitrary order; match by custom_id."""
        request_list = list(requests)
        total = len(request_list)
        results: list[VllmJsonResult] = []

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            futures = {pool.submit(self.run_one, r): r for r in request_list}
            for future in as_completed(futures):
                results.append(future.result())
                if progress is not None:
                    progress(len(results), total)

        return results


def build_default_client(base_url: str, api_key: str, timeout_s: float) -> Any:
    """Construct a fresh ``openai.OpenAI`` client pointed at vLLM.

    Kept as a module-level helper (rather than a method on VllmResource)
    so unit tests can instantiate a client without spinning up Dagster.
    """
    from openai import OpenAI  # noqa: PLC0415 — defer import; openai is an optional dep

    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout_s)
