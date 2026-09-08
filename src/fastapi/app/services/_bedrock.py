"""Shared Amazon Bedrock plumbing for the Cohere model tier (ADR-0022).

Replaces ``_foundry_retry.py``'s hand-rolled httpx backoff. Bedrock is
reached with boto3/SigV4 rather than an API key over httpx, so throttling
and 5xx retry move into botocore's own adaptive retry mode, which already
honours the service's throttling signals. What this module adds on top is
the two things botocore does not do for us:

1. **A process-wide client cache.** Client construction parses botocore's
   JSON service model and is expensive (tens of ms); the clients themselves
   are safe to share across threads. Under Octane-equivalent long-lived
   processes we build each client once. Note this is the *FastAPI* side —
   no Laravel container state is involved.

2. **A deadline guard on the query path.** Same reasoning as
   ``_foundry_retry.with_foundry_retry``'s ``deadline`` argument
   (2026-08-20): the reranker call runs in a thread-pool executor under an
   ``asyncio.wait_for``. ``wait_for`` cancels the *future*, not the thread,
   so a retry loop that outlives the caller's budget burns a pool thread
   and Bedrock quota producing a result nobody will read. Callers on a
   clock pass a budget and get a client whose total retry time fits inside
   it; ingestion callers leave it None and keep unbounded patience.

**Wire shapes in the adapters that use this module are [UNVERIFIED].**
Foundry's contract was confirmed empirically against a live deployment on
2026-07-30 and three of its behaviours were things a reader would not have
assumed. Nothing here has had that treatment yet — Bedrock could not be
reached from the session that wrote it. ``ops/validation/bedrock_probe.py``
exists to close that gap, and its committed report is the gate on trusting
any of these adapters. See ADR-0022 "Verification".
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Region and credentials
# ---------------------------------------------------------------------------
# No key material here, by design. On ECS the task role supplies credentials
# through the container credential provider and boto3's default chain picks
# them up; there is nothing to rotate and nothing to leak into an env var.
# BEDROCK_REGION exists because the Bedrock model catalogue varies by region
# and the models this deployment needs may not live in the region the rest of
# the stack runs in (ADR-0022: confirming that is step 0 of the migration).
DEFAULT_REGION = "us-east-1"


def bedrock_region() -> str:
    return (
        os.environ.get("BEDROCK_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    ).strip()


# ---------------------------------------------------------------------------
# Retired Azure AI Foundry configuration — fail loudly, never silently
# ---------------------------------------------------------------------------
# The pattern is `services/ingest/ocr_engine.py`'s, and it exists for the
# same reason: on 2026-08-21 a worker whose environment still named a retired
# OCR engine ran Tesseract on every page and said nothing, and the silent
# downgrade was only found by reading logs for something else. A deployment
# carrying AZURE_FOUNDRY_* into AWS is in exactly that position — the values
# are well-formed, they just address a resource that no longer exists — so it
# gets an error naming the replacement rather than a confusing 404 later.
_RETIRED_FOUNDRY_ENV = (
    "AZURE_FOUNDRY_ENDPOINT",
    "AZURE_FOUNDRY_API_KEY",
    "AZURE_FOUNDRY_DEPLOYMENT",
    "AZURE_FOUNDRY_EMBED_DEPLOYMENT",
    "AZURE_FOUNDRY_RERANK_DEPLOYMENT",
    "AZURE_FOUNDRY_PARSE_DEPLOYMENT",
)

#: Old backend selector values, mapped to what replaces them.
RETIRED_BACKEND_VALUES: dict[str, str] = {
    "foundry": "bedrock",
    "azure": "bedrock",
}


class RetiredAzureConfiguration(RuntimeError):
    """Raised when Azure AI Foundry configuration is still present on AWS."""


def assert_no_retired_foundry_env(*, context: str) -> None:
    """Raise if any ``AZURE_FOUNDRY_*`` variable is still set.

    Called from each adapter's configuration check so the failure lands at
    the point an operator can act on, naming both the stale variable and its
    replacement.
    """
    present = [name for name in _RETIRED_FOUNDRY_ENV if (os.environ.get(name) or "").strip()]
    if not present:
        return
    raise RetiredAzureConfiguration(
        f"{context}: {', '.join(present)} is set, but Azure AI Foundry was "
        "retired on 2026-09-08 (ADR-0022). Production reaches Cohere through "
        "Amazon Bedrock now. Unset these and set BEDROCK_REGION plus the "
        "BEDROCK_*_MODEL_ID for the capability you are configuring. Leaving "
        "them set addresses a resource that no longer exists."
    )


def reject_retired_backend(value: str, *, setting: str) -> None:
    """Raise if ``value`` is a backend selector that Foundry used to serve."""
    replacement = RETIRED_BACKEND_VALUES.get(value.strip().lower())
    if replacement is None:
        return
    raise RetiredAzureConfiguration(
        f"{setting}={value!r} selects Azure AI Foundry, which was retired on "
        f"2026-09-08 (ADR-0022). Set {setting}={replacement!r}. This is a hard "
        "error rather than a fallback because the fallback would be a model "
        "host that does not exist on AWS, and the failure would surface as a "
        "connection error at first call instead of at startup."
    )


# ---------------------------------------------------------------------------
# Client cache
# ---------------------------------------------------------------------------
_clients: dict[tuple[str, str, int, float], Any] = {}
_clients_lock = threading.Lock()


def _botocore_config(*, max_attempts: int, read_timeout_s: float):
    from botocore.config import Config  # noqa: PLC0415

    return Config(
        region_name=bedrock_region(),
        # "adaptive" adds client-side rate limiting on top of the standard
        # retry set, which is what we want for a full-corpus re-embed: the
        # Foundry equivalent tripped shared-quota 429s that had nothing to do
        # with the request (ADR-0021 gotcha 4), and adaptive mode backs the
        # whole client off rather than retrying each call into the same wall.
        retries={"mode": "adaptive", "max_attempts": max_attempts},
        read_timeout=read_timeout_s,
        connect_timeout=min(10.0, read_timeout_s),
        # Bedrock streaming responses are long-lived; without this botocore
        # can reuse a connection whose peer has already gone away.
        tcp_keepalive=True,
    )


def get_client(
    service: str,
    *,
    max_attempts: int = 4,
    read_timeout_s: float = 30.0,
):
    """Return a cached boto3 client for ``service``.

    ``service`` is a botocore service name — ``bedrock-runtime`` for
    ``InvokeModel`` / ``Converse``, ``bedrock-agent-runtime`` for ``Rerank``,
    ``bedrock`` for control-plane calls such as listing what the region
    actually offers.

    Clients are cached on (service, region, max_attempts, timeout) because
    the two adapters that share a service do not share a budget: the
    reranker sits on the interactive query path with a sub-10s ceiling,
    while the embedder runs under ingestion with none.
    """
    key = (service, bedrock_region(), max_attempts, read_timeout_s)
    client = _clients.get(key)
    if client is not None:
        return client
    with _clients_lock:
        client = _clients.get(key)
        if client is not None:
            return client
        import boto3  # noqa: PLC0415

        client = boto3.client(
            service,
            config=_botocore_config(max_attempts=max_attempts, read_timeout_s=read_timeout_s),
        )
        _clients[key] = client
        logger.info(
            "bedrock: built %s client region=%s max_attempts=%d read_timeout=%.1fs",
            service, bedrock_region(), max_attempts, read_timeout_s,
        )
        return client


def reset_client_cache() -> None:
    """Drop every cached client. Tests only — region is read at build time."""
    with _clients_lock:
        _clients.clear()


def attempts_within_budget(budget_s: float | None, *, floor: int = 1, ceiling: int = 4) -> int:
    """Translate a caller's time budget into a botocore ``max_attempts``.

    botocore has no wall-clock deadline, only an attempt count, so this is
    the closest honest mapping: assume the first attempt plus each backoff
    costs roughly a second and never promise more attempts than the budget
    can pay for. ``None`` means no clock — ingestion — and gets the ceiling.
    """
    if budget_s is None:
        return ceiling
    return max(floor, min(ceiling, int(budget_s // 2)))


__all__ = [
    "DEFAULT_REGION",
    "RETIRED_BACKEND_VALUES",
    "RetiredAzureConfiguration",
    "assert_no_retired_foundry_env",
    "attempts_within_budget",
    "bedrock_region",
    "get_client",
    "reject_retired_backend",
    "reset_client_cache",
]
