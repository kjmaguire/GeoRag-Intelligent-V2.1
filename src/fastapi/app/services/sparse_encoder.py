"""SPLADE++ sparse encoder -- singleton loader for GeoRAG hybrid retrieval.

This module provides the shared SPLADE++ sparse encoder used at query time
(FastAPI) and at index time (Dagster index_reports).

KEEP IN SYNC -- see counterpart in:
    src/dagster/georag_dagster/assets/sparse_encoder.py

Both copies must be identical. When changing this file, change the other too.
Last sync: 2026-04-21 (Module 4 Chunk 2)

Model choice
------------
naver/splade-cocondenser-ensembledistil is the stable SPLADE++ variant
(2022-01 release). It produces sparse token-weight vectors that complement
dense semantic embeddings for keyword-exact retrieval -- critical for
geological queries that include specific identifiers (hole IDs like
"PLS-22-08", NTS tile codes, commodity symbols like "u3o8").

The model is pinned by HuggingFace revision SHA to prevent silent weight
drift. When a new model version is approved (after Milestone 2 benchmarking),
update SPARSE_MODEL_REVISION and SPARSE_MODEL_VERSION here AND in the
Dagster counterpart.

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

Lifespan pre-warm
-----------------
Calling encode_sparse() during FastAPI lifespan startup triggers the
lru_cache load, warming the model before the first real request. See
main.py for the pre-warm call.

Dagster note
------------
In Dagster workers, the lru_cache persists for the lifetime of the Dagster
daemon/executor process. For multi-process execution (the default), each
worker subprocess loads its own model copy. This is the expected behaviour.
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model identity -- pinned to a specific HuggingFace commit SHA.
# Checked: 2026-04-21 against https://huggingface.co/naver/splade-cocondenser-ensembledistil
# ---------------------------------------------------------------------------
SPARSE_MODEL_NAME = "naver/splade-cocondenser-ensembledistil"
SPARSE_MODEL_REVISION = "49cf4c7b0db5b870a401ddf5e2669993ef3699c7"
# Short form stored in answer_runs.sparse_model_version and Qdrant payload parser_version
SPARSE_MODEL_VERSION = "splade-cocondenser-ensembledistil@49cf4c7b"


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
    )
    model = AutoModelForMaskedLM.from_pretrained(
        SPARSE_MODEL_NAME,
        revision=SPARSE_MODEL_REVISION,
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


def encode_sparse(text: str) -> dict[int, float]:
    """Encode text into a SPLADE++ sparse vector.

    Uses SPLADE aggregation: max-pool log(1 + ReLU(logits)) over the
    sequence dimension, then extract non-zero (token_id, weight) pairs.

    Args:
        text: Raw text to encode. Truncated to 512 tokens if longer.

    Returns:
        Dict mapping vocabulary token IDs to positive weights.
        Only non-zero entries are returned (typically 50-500 terms).
        Ready for qdrant_client.models.SparseVector(indices=..., values=...).

    Example:
        >>> v = encode_sparse("uranium grade intercept PLS-22-08")
        >>> len(v)   # 50-500
        >>> max(v.values())  # > 0.0
    """
    import torch

    tokenizer, model = _get_sparse_model()

    inputs = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=512,
        padding=False,
    )

    if torch.cuda.is_available():
        inputs = {k: v.cuda() for k, v in inputs.items()}

    with torch.no_grad():
        logits = model(**inputs).logits  # shape: (1, seq_len, vocab_size)

    # SPLADE aggregation:
    #   1. ReLU: zero out negative logits
    #   2. log(1 + x): compress dynamic range
    #   3. max over sequence: take the strongest activation per token
    attention_mask = inputs["attention_mask"]  # shape: (1, seq_len)
    # Mask padded positions before max-pool by zeroing their contributions
    weights = torch.max(
        torch.log1p(torch.relu(logits)) * attention_mask.unsqueeze(-1),
        dim=1,
    ).values  # shape: (1, vocab_size)

    # Extract non-zero vocabulary entries
    nz = weights[0].nonzero(as_tuple=False).squeeze(-1)
    if nz.numel() == 0:
        logger.warning("encode_sparse: produced 0 non-zero terms for text=%r", text[:80])
        return {}

    indices: list[int] = nz.tolist()
    values: list[float] = weights[0][nz].cpu().float().tolist()

    return dict(zip(indices, values))


def encode_sparse_batch(texts: list[str], batch_size: int = 32) -> list[dict[int, float]]:
    """Encode a batch of texts into SPLADE++ sparse vectors.

    More efficient than calling encode_sparse() in a loop when indexing
    many documents, because the model's attention mechanism can be batched.

    Args:
        texts: List of raw text strings.
        batch_size: Number of texts to encode per forward pass.

    Returns:
        List of sparse dicts in the same order as input texts.
    """
    import torch

    tokenizer, model = _get_sparse_model()
    results: list[dict[int, float]] = []

    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        inputs = tokenizer(
            batch,
            return_tensors="pt",
            truncation=True,
            max_length=512,
            padding=True,
        )

        if torch.cuda.is_available():
            inputs = {k: v.cuda() for k, v in inputs.items()}

        with torch.no_grad():
            logits = model(**inputs).logits  # (batch, seq_len, vocab)

        attention_mask = inputs["attention_mask"]
        weights = torch.max(
            torch.log1p(torch.relu(logits)) * attention_mask.unsqueeze(-1),
            dim=1,
        ).values  # (batch, vocab)

        for i in range(weights.shape[0]):
            nz = weights[i].nonzero(as_tuple=False).squeeze(-1)
            if nz.numel() == 0:
                results.append({})
            else:
                idx: list[int] = nz.tolist()
                vals: list[float] = weights[i][nz].cpu().float().tolist()
                results.append(dict(zip(idx, vals)))

    return results
