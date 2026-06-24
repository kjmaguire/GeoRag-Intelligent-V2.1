"""Cross-encoder reranker singleton for GeoRAG hybrid retrieval.

Module 4 Phase B Chunk 3 -- B6 reranker wiring.

Model
-----
BAAI/bge-reranker-base (Apache 2.0, ~278 MB).
Pinned to revision SHA to prevent silent weight drift.
The model produces raw float scores (higher = more relevant).
No sigmoid transform is applied here -- callers use the raw score for
thresholding and may apply sigmoid themselves for [0,1] normalisation.

Revision pinning
----------------
SHA: 2cfc18c9415c912f9d8155881c133215df768a70
Confirmed 2026-05-14 against HuggingFace API (doc-phase 176).
Previous pin `5ccf1b81c57ff625b3e4b7ab15481d6e2ee9bc56` was no longer
accessible upstream — the SHA produced a `.no_exist/config.json`
marker in the HF cache, causing s-t 5.5.0 to fail with
"Unrecognized model in BAAI/bge-reranker-base. Should have a
`model_type` key in its config.json". Re-pinning to the current main
HEAD SHA fixed the load path; both chat retrieval and eval Layer 5
chunk-provenance gating now use the cross-encoder.

If the model is updated upstream, update RERANKER_REVISION and RERANKER_VERSION
together.  The version string is persisted to answer_runs.reranker_version so
any shift in reranker behaviour is traceable via the audit trail.

Version string
--------------
RERANKER_VERSION = "bge-reranker-base@<first 8 chars of SHA>"
Used by the orchestrator to populate answer_runs.reranker_version.

Top-k per query class (spec B6)
--------------------------------
RERANKER_TOP_K_BY_CLASS maps each spec query class to the number of candidates
to keep after reranking.  These are intentional defaults -- tweak via Phase C
benchmarking when golden query numbers are available.

    factual:     20  (moderate depth for factual lookups)
    spatial:     30  (wider pool -- many collars can be relevant)
    document:    15  (higher precision for report-section synthesis)
    computation: 10  (tight -- computation needs the top few exact matches)
    viz:         30  (spatial visualisation needs a wide candidate pool)
    unknown:     20  (safe default)

Timeout
-------
RERANKER_TIMEOUT_S = 2.0 seconds for a batch of up to 50 candidates on CPU.
If the reranker exceeds this budget, the orchestrator logs + continues with
RRF-ordered results (no hard failure per spec B6 fallback policy).

Singleton
---------
_get_reranker() is decorated with @lru_cache(maxsize=1) -- a single
CrossEncoder instance is shared per worker process.  The lifespan hook in
main.py pre-warms the singleton at startup.  Callers that need the version
string import RERANKER_VERSION directly without loading the model.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model identity -- pin by HuggingFace revision SHA
# ---------------------------------------------------------------------------

RERANKER_MODEL_NAME = "BAAI/bge-reranker-base"
# Doc-phase 176 — re-pinned from `5ccf1b81...` (no longer accessible
# upstream) to current main HEAD as of 2026-05-14. See module docstring
# for context.
RERANKER_REVISION = "2cfc18c9415c912f9d8155881c133215df768a70"
RERANKER_VERSION = f"bge-reranker-base@{RERANKER_REVISION[:8]}"

# ---------------------------------------------------------------------------
# Per-query-class top-k defaults (spec B6)
# ---------------------------------------------------------------------------

RERANKER_TOP_K_BY_CLASS: dict[str, int] = {
    "factual":     20,
    "spatial":     30,
    "document":    15,
    "computation": 10,
    "viz":         30,
    "unknown":     20,
}

# Default for callers that do not supply a query class.
RERANKER_TOP_K_DEFAULT = 20

# ---------------------------------------------------------------------------
# Timeout budget for a single reranker batch (seconds, CPU-bound)
# ---------------------------------------------------------------------------

RERANKER_TIMEOUT_S = 2.0


# ---------------------------------------------------------------------------
# Shared reranker sidecar (2026-06-24)
# ---------------------------------------------------------------------------
# Each uvicorn worker used to load its OWN CrossEncoder copy (6 workers → 6×
# ~1-1.5 GiB → OOM-killed the container under the 10 GiB limit). When
# RERANKER_SERVICE_URL is set, get_reranker_or_none() instead returns a thin
# HTTP proxy to the dedicated single-process `reranker` sidecar that hosts ONE
# model copy — the workers share it. Unset (the default) keeps the in-process
# load, so tests and the sidecar itself behave exactly as before.
RERANKER_SERVICE_URL = (os.environ.get("RERANKER_SERVICE_URL") or "").strip()
# Outer budget for the sidecar HTTP round-trip. Generous on purpose: the
# orchestrator already wraps the predict() call in its own RERANKER_TIMEOUT_S
# wait_for, so that fires first and this only guards a wedged sidecar.
RERANKER_SERVICE_TIMEOUT_S = float(os.environ.get("RERANKER_SERVICE_TIMEOUT_S", "10"))


class _RemoteReranker:
    """HTTP proxy with the only CrossEncoder method callers use: ``predict``.

    Mirrors ``CrossEncoder.predict(list[(query, passage)]) -> list[float]`` by
    POSTing the pairs to the reranker sidecar. Kept deliberately minimal so it
    is a drop-in for ``get_reranker_or_none()`` consumers (orchestrator +
    eval Layer 5). A wedged/absent sidecar raises here; callers already treat a
    reranker failure as a soft-degrade to RRF order (spec B6 fallback).
    """

    def __init__(self, base_url: str, timeout_s: float) -> None:
        self._url = base_url.rstrip("/") + "/rerank"
        self._timeout_s = timeout_s

    def predict(self, pairs: "list[tuple[str, str]]") -> list[float]:
        import httpx  # noqa: PLC0415

        payload = {"pairs": [[str(q), str(p)] for q, p in pairs]}
        resp = httpx.post(self._url, json=payload, timeout=self._timeout_s)
        resp.raise_for_status()
        return [float(s) for s in resp.json()["scores"]]


@lru_cache(maxsize=1)
def _get_reranker() -> "CrossEncoder":
    """Load and return the BGE reranker singleton (cached per worker process).

    Raises:
        ImportError: if sentence-transformers is not installed.
        OSError: if the model files cannot be downloaded / found.

    The caller (lifespan hook) should catch and log exceptions -- a reranker
    failure degrades quality but must not prevent service startup.
    """
    import os  # noqa: PLC0415
    import torch  # noqa: PLC0415
    from sentence_transformers import CrossEncoder  # noqa: PLC0415

    # Latency-fix follow-up — explicit torch thread count for the CPU
    # CrossEncoder. PyTorch uses its OWN intra-op thread count separate
    # from OpenMP / OMP_NUM_THREADS — in containers it defaults to ~3,
    # which makes bge-reranker-base take ~700ms/pair on 1.4k-token
    # chunks and consistently blow the per-branch timeout. Setting it
    # explicitly to 10 (or RERANKER_TORCH_THREADS env override) drops
    # per-pair latency to ~200-250 ms.
    _desired_threads = int(os.environ.get("RERANKER_TORCH_THREADS", "10"))
    try:
        torch.set_num_threads(_desired_threads)
    except RuntimeError:
        # PyTorch raises if threads were already set elsewhere; not fatal.
        pass
    logger.info(
        "Reranker torch threads: requested=%d actual=%d (interop=%d)",
        _desired_threads,
        torch.get_num_threads(),
        torch.get_num_interop_threads(),
    )

    # ADR-0010 §5e — RERANKER_MODEL_PATH override lets the operator A/B test
    # a LoRA-tuned candidate against the stock baseline without rebuilding
    # the image. Set to a local directory containing config.json +
    # model.safetensors (the merged-and-unloaded artifact from
    # scripts/eval_reranker_lora.py); unset/empty falls back to the
    # pinned HuggingFace identity. Used by the §5e training cycle's
    # out-of-distribution sanity check against golden_queries.
    local_path = (os.environ.get("RERANKER_MODEL_PATH") or "").strip()
    if local_path:
        logger.info(
            "Loading reranker from LOCAL PATH (RERANKER_MODEL_PATH override): %s",
            local_path,
        )
        model = CrossEncoder(local_path, device="cpu")
        active_version = f"local:{local_path}"
    else:
        logger.info(
            "Loading reranker: %s revision=%s",
            RERANKER_MODEL_NAME, RERANKER_REVISION,
        )
        model = CrossEncoder(
            RERANKER_MODEL_NAME,
            revision=RERANKER_REVISION,
            device="cpu",
        )
        active_version = RERANKER_VERSION

    # Warm-up pass so the first real query doesn't pay JIT compilation cost.
    model.predict([("warm up query", "warm up geological document passage")])
    logger.info("Reranker ready: %s", active_version)
    return model


def get_reranker_or_none() -> "CrossEncoder | _RemoteReranker | None":
    """Return the reranker (local singleton, remote proxy, or None).

    When RERANKER_SERVICE_URL is set, returns an HTTP proxy to the shared
    `reranker` sidecar — no local model is loaded in this process. Otherwise
    loads the in-process CrossEncoder singleton. All exceptions are caught so
    callers can handle the absent-reranker path (RRF order fallback) without
    try/except boilerplate. Env is read fresh each call so it stays
    monkeypatchable in tests.
    """
    service_url = (os.environ.get("RERANKER_SERVICE_URL") or "").strip()
    if service_url:
        timeout_s = float(os.environ.get("RERANKER_SERVICE_TIMEOUT_S", "10"))
        return _RemoteReranker(service_url, timeout_s)
    try:
        return _get_reranker()
    except Exception:
        logger.exception(
            "reranker: failed to load %s -- rerank step will be skipped",
            RERANKER_MODEL_NAME,
        )
        return None


def top_k_for_class(query_class: str | None) -> int:
    """Return the per-query-class reranker top-k.

    Args:
        query_class: One of the spec query classes ("factual", "spatial",
                     "document", "computation", "viz", "unknown"), or None
                     to use the global default.

    Returns:
        Integer top-k (number of candidates to keep post-rerank).
    """
    if query_class is None:
        return RERANKER_TOP_K_DEFAULT
    return RERANKER_TOP_K_BY_CLASS.get(query_class, RERANKER_TOP_K_DEFAULT)
