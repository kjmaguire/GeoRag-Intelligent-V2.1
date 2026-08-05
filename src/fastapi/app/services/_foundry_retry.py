"""Retry/backoff for Azure AI Foundry HTTP calls (Cohere Embed/Rerank).

Foundry TPM quota is shared per-subscription across every deployment in the
resource (confirmed empirically during the canadacentral migration — see the
plan's C5/region-migration notes), so a burst of calls — most notably C8's
mandatory full-corpus re-embed — can trip a transient 429 that has nothing
to do with a real error. Before this module existed, ``passage_embedder.py``
treated any exception from an embed call, including a 429, as a permanent
per-batch failure (``passages_skipped += len(batch)``, no retry) — silently
dropping passages from a corpus re-embed rather than recovering from a
rate-limit blip.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def with_foundry_retry(
    do_post: Callable[[], httpx.Response],
    *,
    label: str,
    max_retries: int = 4,
    max_backoff_s: float = 30.0,
) -> httpx.Response:
    """Call ``do_post()`` and retry on 429/5xx with backoff.

    Honors a numeric ``Retry-After`` header when present (Cohere/Azure send
    this on 429); otherwise backs off 2s, 4s, 8s, 16s (capped at
    ``max_backoff_s``). Raises the underlying ``httpx.HTTPStatusError`` via
    ``raise_for_status()`` once ``max_retries`` is exhausted or the response
    isn't retryable at all (4xx other than 429).
    """
    attempt = 0
    while True:
        resp = do_post()
        if resp.status_code not in _RETRYABLE_STATUS or attempt >= max_retries:
            resp.raise_for_status()
            return resp

        retry_after = resp.headers.get("retry-after")
        try:
            delay = float(retry_after) if retry_after else 2.0 * (2**attempt)
        except ValueError:
            delay = 2.0 * (2**attempt)
        delay = min(delay, max_backoff_s)

        logger.warning(
            "%s: status=%d attempt=%d/%d backing off %.1fs",
            label, resp.status_code, attempt + 1, max_retries, delay,
        )
        time.sleep(delay)
        attempt += 1
