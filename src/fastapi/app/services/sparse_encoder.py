"""SPLADE++ sparse encoder -- singleton loader for GeoRAG hybrid retrieval.

This module provides the shared SPLADE++ sparse encoder used at query time
(FastAPI) and at index time (the Hatchet ingest workers, via
``passage_embedder``). It is the only copy: the Dagster tree that carried a
forked duplicate was deleted 2026-08-28.

Model choice
------------
naver/splade-cocondenser-ensembledistil is the stable SPLADE++ variant
(2022-01 release). It produces sparse token-weight vectors that complement
dense semantic embeddings for keyword-exact retrieval -- critical for
geological queries that include specific identifiers (hole IDs like
"PLS-22-08", NTS tile codes, commodity symbols like "u3o8").

The model is pinned by HuggingFace revision SHA to prevent silent weight
drift. When a new model version is approved (after Milestone 2 benchmarking),
update SPARSE_MODEL_REVISION and SPARSE_MODEL_VERSION here.

Memory footprint
----------------
The SPLADE model (BERT-base-sized, ~110 M parameters) occupies:
  - CPU / fp32: ~440 MB per process
  - GPU / fp16: ~220 MB per process

FastAPI runs 4 Uvicorn workers -> 4 x ~440 MB = ~1.76 GB SPLADE alone.
Combined with the dense encoder (~100 MB) and OS overhead, the container
requires at least 4 GB. docker-compose.yml is configured for 6 GB.

Usage
-----
    from app.services.sparse_encoder import encode_sparse

    sparse_vec: dict[int, float] = encode_sparse("drillhole assay results")
    # -> {1234: 0.87, 5678: 1.23, ...}  (token_id -> weight, non-zero only)
    # Ready to pass to qdrant_client models.SparseVector(
    #     indices=list(sparse_vec.keys()),
    #     values=list(sparse_vec.values()),
    # )

Thread safety
-------------
_get_sparse_model() uses functools.lru_cache(maxsize=1) which is NOT
thread-safe in the general case, but is safe here because:
1. Python's GIL serialises the first call across threads.
2. lru_cache internally double-checks before storing (effectively once-only).
3. The model is read-only after load -- no mutable shared state.

If you remove the GIL (free-threaded CPython 3.13+), add an explicit
threading.Lock around the first call.

Forward passes, by contrast, ARE serialised: ``_FORWARD_LOCK`` below. See
"Peak memory" further down for why.

Lifespan pre-warm
-----------------
Calling encode_sparse() during FastAPI lifespan startup triggers the
lru_cache load, warming the model before the first real request. See
main.py for the pre-warm call.
"""

from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache
from typing import Any

from app.agent.log_safe import query_hash, text_shape

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model identity -- pinned to a specific HuggingFace commit SHA.
# Checked: 2026-04-21 against https://huggingface.co/naver/splade-cocondenser-ensembledistil
# ---------------------------------------------------------------------------
SPARSE_MODEL_NAME = "naver/splade-cocondenser-ensembledistil"
SPARSE_MODEL_REVISION = "49cf4c7b0db5b870a401ddf5e2669993ef3699c7"
# Short form of the pinned revision. NOTE: nothing persists it today --
# answer_runs.sparse_model_version is declared on the model but never written.
SPARSE_MODEL_VERSION = "splade-cocondenser-ensembledistil@49cf4c7b"


# ---------------------------------------------------------------------------
# Shared-sidecar routing (FastAPI query workers only)
# ---------------------------------------------------------------------------
# When SPARSE_SERVICE_URL is set, encode_sparse{,_batch} POST to the shared
# SPLADE sidecar (app.sparse_service) over a localhost hop instead of loading a
# per-process model. NOT on the sidecar itself (which would proxy to itself).
#
# The original rule said "ONLY on the FastAPI service, NOT on the index
# pipeline, which keeps its own local model for throughput". The pipeline
# that rule named (Dagster) is gone and its successor, the hatchet-worker,
# DOES get this URL on AWS (deploy/aws/terraform/config.tf sets it in common_environment). That
# is a live trade rather than an oversight: one shared 440 MB model instead of
# a second copy resident in the worker, paid for with a network hop per
# passage during bulk ingest. If ingest throughput ever becomes the
# constraint, unsetting it on hatchet-worker is the lever. The sidecar runs THIS SAME
# code with the URL unset, so the produced vectors are identical. Sibling of the
# embedding sidecar (app/services/embedding.py). It is inert wherever
# SPARSE_SERVICE_URL is unset.
SPARSE_SERVICE_URL = (os.environ.get("SPARSE_SERVICE_URL") or "").strip()


class SparseEncoderUnavailable(RuntimeError):
    """The sparse leg of hybrid retrieval could not be computed.

    Raised by callers that wrap encode_sparse, not by encode_sparse itself --
    the underlying failures are httpx transport errors, HTTP statuses and
    model-load errors, and flattening them here would lose the cause.

    It exists so that "the sparse sidecar is unreachable" and "Qdrant
    returned nothing" stop being the same observable event. Global Invariant
    11 says a sparse failure must not degrade to a dense-only query, and that
    half holds; what did not hold is anyone being TOLD. A reclaimed Spot task
    (`sparse` runs desired=1) silently stopped hole IDs and sample numbers
    matching, while every answer still streamed and nothing reported it.
    """


#: One pooled, keep-alive client for the sidecar hop. ``httpx.post`` builds a
#: client, opens a connection and tears both down on EVERY call -- a TCP (and,
#: on AWS, TLS) handshake in the middle of every query's retrieval stage
#: (audit item 26). ``httpx.Client`` is thread-safe, which matters because
#: encode_sparse runs in the default executor.
_REMOTE_CLIENT: Any = None
_REMOTE_CLIENT_LOCK = threading.Lock()


def _get_remote_client() -> Any:
    """The shared sidecar client, built on first use."""
    global _REMOTE_CLIENT  # noqa: PLW0603
    if _REMOTE_CLIENT is None:
        import httpx  # noqa: PLC0415

        with _REMOTE_CLIENT_LOCK:
            if _REMOTE_CLIENT is None:
                _REMOTE_CLIENT = httpx.Client(
                    limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
                )
    return _REMOTE_CLIENT


def _remote_encode_sparse(texts: list[str]) -> list[dict[int, float]]:
    """Encode via the shared SPLADE sidecar. JSON object keys are strings, so
    the int token-ids round-trip as strings and are restored to int here."""
    import httpx  # noqa: PLC0415

    from app.sidecar_auth import SERVICE_KEY_HEADERS  # noqa: PLC0415

    timeout_s = float(os.environ.get("SPARSE_SERVICE_TIMEOUT_S", "30") or "30")
    url = f"{SPARSE_SERVICE_URL.rstrip('/')}/sparse"
    try:
        resp = _get_remote_client().post(
            url, json={"texts": texts}, timeout=timeout_s, headers=SERVICE_KEY_HEADERS,
        )
    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as exc:
        logger.debug("sparse sidecar connection stale (%s); retrying once", exc)
        # A kept-alive socket the sidecar closed (a Spot reclaim, a restart)
        # surfaces here on first reuse. One retry on a fresh connection; a
        # real outage fails again and is reported as such.
        resp = _get_remote_client().post(
            url, json={"texts": texts}, timeout=timeout_s, headers=SERVICE_KEY_HEADERS,
        )
    resp.raise_for_status()
    return [{int(k): v for k, v in d.items()} for d in resp.json()["sparse"]]


# ---------------------------------------------------------------------------
# Singleton loader
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_sparse_model():  # type: ignore[return]
    """Load and cache the SPLADE++ tokenizer and model.

    Returns:
        (tokenizer, model) tuple. Model is moved to GPU (fp16) if CUDA is
        available, otherwise stays on CPU (fp32).

    The lru_cache ensures the ~440 MB model is loaded only once per
    process, regardless of how many times encode_sparse() is called.
    """
    import torch
    from transformers import AutoModelForMaskedLM, AutoTokenizer

    logger.info(
        "Loading SPLADE++ model %s @ %s...",
        SPARSE_MODEL_NAME,
        SPARSE_MODEL_REVISION[:8],
    )

    tokenizer = AutoTokenizer.from_pretrained(
        SPARSE_MODEL_NAME,
        revision=SPARSE_MODEL_REVISION,
        trust_remote_code=False,
    )
    model = AutoModelForMaskedLM.from_pretrained(
        SPARSE_MODEL_NAME,
        revision=SPARSE_MODEL_REVISION,
        trust_remote_code=False,
    )
    model.eval()

    if torch.cuda.is_available():
        # fp16 halves VRAM footprint with negligible quality loss on
        # sparse token-weight outputs.
        model = model.half().cuda()
        logger.info(
            "SPLADE++ loaded on CUDA (fp16), device=%s",
            torch.cuda.get_device_name(0),
        )
    else:
        logger.info(
            "SPLADE++ loaded on CPU (fp32) -- expect 15-60ms per encode"
        )

    return tokenizer, model


# ---------------------------------------------------------------------------
# Long-text windowing (audit RAG-10, 2026-09-29)
# ---------------------------------------------------------------------------
# Every encode used to run ONE forward pass with truncation=True,
# max_length=512. Ingest chunks are WINDOW_CHARS=5000 characters
# (pdf_report.py) — 1,250+ wordpieces, more with hole IDs and assay numbers
# — and passage_embedder encodes contextualized_content, a generated header
# PLUS the text. So the sparse leg saw roughly the first third of each
# chunk: hole IDs, sample numbers and NTS codes in the rest never reached
# it, and identifier_boost widened a pool that could not contain them.
# Nothing errored.
#
# SPLADE's aggregation is already a max-pool over positions, so encoding
# overlapping 512-token windows and max-pooling across them is the same
# operation over the whole text. Short inputs (every query) take the
# single-pass path unchanged, so query vectors do not move.

#: Content tokens per window: 512 minus [CLS] and [SEP].
_WINDOW_CONTENT_TOKENS = 510
#: Window start stride. 510 - 384 = 126 tokens of overlap, so a term split
#: by a boundary is seen whole in one window.
_WINDOW_STRIDE = 384
#: Windows per forward pass — bounds peak memory on a very long input.
#: Was 8 until 2026-09-30; see "Peak memory" below.
_WINDOWS_PER_FORWARD = 4

#: Texts per forward pass in encode_sparse_batch. Was 32; see below.
_SHORT_BATCH_SIZE = 8


# ---------------------------------------------------------------------------
# Peak memory (2026-09-30)
# ---------------------------------------------------------------------------
# SPLADE's output is a logit per position per VOCABULARY entry: batch x
# seq_len x 30,522 float32. At 8 windows x 512 positions that is 500 MB for
# ONE tensor, and the aggregation used to be written out-of-place —
# relu(), log1p() and the mask multiply each allocated another 500 MB, so a
# single long passage peaked near 2 GB above the model. The sidecar runs
# every request in its own thread (asyncio.to_thread) and the embed sweep
# keeps EMBED_CONCURRENCY=3 requests in flight, so the first real ingest in
# production (Red Star, 2026-09-30) OOM-killed the 2 GB sparse task twice
# and every chunk in flight went back to the queue.
#
# Three changes, any one of which would have helped:
#   * the aggregation is done IN PLACE on the logits (_splade_weights), so a
#     forward pass holds one vocab-sized tensor, not four;
#   * fewer windows / texts per forward pass (above), which halves or
#     quarters that tensor;
#   * forward passes are serialised by _FORWARD_LOCK. On CPU torch already
#     spreads one forward across every core, so running two at once buys no
#     throughput — it only multiplies peak memory by the number of
#     concurrent requests, which nothing bounds.
_FORWARD_LOCK = threading.Lock()


def _splade_weights(logits: Any, attention_mask: Any) -> Any:
    """``log(1 + ReLU(logits))`` with padded positions zeroed, IN PLACE.

    Overwrites ``logits`` and returns it. Mathematically identical to the
    out-of-place ``torch.log1p(torch.relu(logits)) * mask.unsqueeze(-1)``;
    it just does not allocate three more vocab-sized tensors to get there.
    """
    return logits.relu_().log1p_().mul_(attention_mask.unsqueeze(-1).to(logits.dtype))


def _window_token_ids(tokenizer: Any, text: str) -> list[list[int]] | None:
    """Content-token windows for ``text``, or None if it fits one pass."""
    ids = tokenizer(text, add_special_tokens=False, truncation=False)["input_ids"]
    if len(ids) <= _WINDOW_CONTENT_TOKENS:
        return None
    windows: list[list[int]] = []
    start = 0
    while True:
        windows.append(ids[start:start + _WINDOW_CONTENT_TOKENS])
        if start + _WINDOW_CONTENT_TOKENS >= len(ids):
            return windows
        start += _WINDOW_STRIDE


def _encode_windows(tokenizer: Any, model: Any, windows: list[list[int]]) -> dict[int, float]:
    """SPLADE vector for a long text: max-pool over every window's positions."""
    import torch

    cls_id = tokenizer.cls_token_id
    sep_id = tokenizer.sep_token_id
    pad_id = tokenizer.pad_token_id or 0
    merged: dict[int, float] = {}
    for start in range(0, len(windows), _WINDOWS_PER_FORWARD):
        seqs = [[cls_id, *w, sep_id] for w in windows[start:start + _WINDOWS_PER_FORWARD]]
        width = max(len(s) for s in seqs)
        input_ids = torch.tensor([s + [pad_id] * (width - len(s)) for s in seqs])
        attention_mask = torch.tensor(
            [[1] * len(s) + [0] * (width - len(s)) for s in seqs]
        )
        if torch.cuda.is_available():
            input_ids = input_ids.cuda()
            attention_mask = attention_mask.cuda()
        with _FORWARD_LOCK, torch.no_grad():
            logits = model(input_ids=input_ids, attention_mask=attention_mask).logits
            pooled = _splade_weights(logits, attention_mask).amax(dim=(0, 1))  # windows AND positions
            del logits
        nz = pooled.nonzero(as_tuple=False).squeeze(-1)
        for tid, w in zip(nz.tolist(), pooled[nz].cpu().float().tolist(), strict=False):
            if w > merged.get(tid, 0.0):
                merged[tid] = w
    return merged


def encode_sparse(text: str) -> dict[int, float]:
    """Encode text into a SPLADE++ sparse vector.

    Uses SPLADE aggregation: max-pool log(1 + ReLU(logits)) over the
    sequence dimension, then extract non-zero (token_id, weight) pairs.

    Args:
        text: Raw text to encode. Longer than 510 content tokens → encoded
            in overlapping windows and max-pooled (RAG-10); it used to be
            truncated at 512.

    Returns:
        Dict mapping vocabulary token IDs to positive weights.
        Only non-zero entries are returned (typically 50-500 terms).
        Ready for qdrant_client.models.SparseVector(indices=..., values=...).

    Example:
        >>> v = encode_sparse("uranium grade intercept PLS-22-08")
        >>> len(v)   # 50-500
        >>> max(v.values())  # > 0.0
    """
    if SPARSE_SERVICE_URL:
        return _remote_encode_sparse([text])[0]

    import torch

    tokenizer, model = _get_sparse_model()

    windows = _window_token_ids(tokenizer, text)
    if windows is not None:
        return _encode_windows(tokenizer, model, windows)

    inputs = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=512,
        padding=False,
    )

    if torch.cuda.is_available():
        inputs = {k: v.cuda() for k, v in inputs.items()}

    # SPLADE aggregation:
    #   1. ReLU: zero out negative logits
    #   2. log(1 + x): compress dynamic range
    #   3. max over sequence: take the strongest activation per token
    # Padded positions are zeroed before the max-pool (_splade_weights).
    with _FORWARD_LOCK, torch.no_grad():
        logits = model(**inputs).logits  # shape: (1, seq_len, vocab_size)
        weights = _splade_weights(logits, inputs["attention_mask"]).amax(dim=1)  # (1, vocab_size)
        del logits

    # Extract non-zero vocabulary entries
    nz = weights[0].nonzero(as_tuple=False).squeeze(-1)
    if nz.numel() == 0:
        # NOT the text. This runs on the user's expanded query
        # (tools.py) and on document passage text (passage_embedder.py)
        # -- customer exploration data -- and stdout here lands in
        # ContainerAppConsoleLogs_CL for 30 days. `log_safe` was written
        # to close this exact leak in the agent tree; the embedding tree
        # was never swept.
        #
        # The shape summary keeps what a debugger actually needs. An
        # empty SPLADE vector is a property of the input's CHARACTER
        # CLASSES, not its meaning: symbol-heavy or non-Latin text is
        # what produces one.
        logger.warning(
            "encode_sparse: produced 0 non-zero terms for query_hash=%s %s",
            query_hash(text), text_shape(text),
        )
        return {}

    indices: list[int] = nz.tolist()
    values: list[float] = weights[0][nz].cpu().float().tolist()

    return dict(zip(indices, values, strict=False))


def encode_sparse_batch(texts: list[str], batch_size: int = _SHORT_BATCH_SIZE) -> list[dict[int, float]]:
    """Encode a batch of texts into SPLADE++ sparse vectors.

    More efficient than calling encode_sparse() in a loop when indexing
    many documents, because the model's attention mechanism can be batched.

    Args:
        texts: List of raw text strings.
        batch_size: Number of texts to encode per forward pass.

    Returns:
        List of sparse dicts in the same order as input texts.
    """
    if SPARSE_SERVICE_URL:
        return _remote_encode_sparse(texts)

    tokenizer, model = _get_sparse_model()
    results: list[dict[int, float]] = []

    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        # RAG-10: long texts are windowed individually; the rest keep the
        # single batched forward below. Order is preserved.
        batch_windows = [_window_token_ids(tokenizer, t) for t in batch]
        short = [t for t, w in zip(batch, batch_windows, strict=True) if w is None]
        short_vecs = iter(_encode_short_batch(tokenizer, model, short) if short else [])
        for w in batch_windows:
            results.append(
                next(short_vecs) if w is None else _encode_windows(tokenizer, model, w)
            )

    return results


def _encode_short_batch(tokenizer: Any, model: Any, batch: list[str]) -> list[dict[int, float]]:
    """One batched forward over texts that each fit a single 512 window."""
    import torch

    results: list[dict[int, float]] = []
    if batch:
        inputs = tokenizer(
            batch,
            return_tensors="pt",
            truncation=True,
            max_length=512,
            padding=True,
        )

        if torch.cuda.is_available():
            inputs = {k: v.cuda() for k, v in inputs.items()}

        with _FORWARD_LOCK, torch.no_grad():
            logits = model(**inputs).logits  # (batch, seq_len, vocab)
            weights = _splade_weights(logits, inputs["attention_mask"]).amax(dim=1)  # (batch, vocab)
            del logits

        for i in range(weights.shape[0]):
            nz = weights[i].nonzero(as_tuple=False).squeeze(-1)
            if nz.numel() == 0:
                results.append({})
            else:
                idx: list[int] = nz.tolist()
                vals: list[float] = weights[i][nz].cpu().float().tolist()
                results.append(dict(zip(idx, vals, strict=False)))

    return results
