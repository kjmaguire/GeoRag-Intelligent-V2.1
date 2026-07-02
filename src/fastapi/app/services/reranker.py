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

# Version string of the backend _get_reranker() ACTUALLY loaded — set when the
# singleton is built. RERANKER_VERSION above is only the bge default; reporting
# it unconditionally (as the sidecar did pre-2026-07-02) mislabels the
# qwen3_causal and RERANKER_MODEL_PATH deployments in answer_runs lineage.
_ACTIVE_VERSION: str | None = None


def active_reranker_version() -> str:
    """Version of the loaded reranker backend (derived from env if not loaded).

    Mirrors the branch order in :func:`_get_reranker` so the pre-load answer
    matches what a subsequent load will report.
    """
    if _ACTIVE_VERSION is not None:
        return _ACTIVE_VERSION
    model_path = (os.environ.get("RERANKER_MODEL_PATH") or "").strip()
    if RERANKER_BACKEND == "qwen3_causal":
        return f"qwen3-causal:{model_path or QWEN3_RERANKER_MODEL}"
    if model_path:
        return f"local:{model_path}"
    return RERANKER_VERSION

# ---------------------------------------------------------------------------
# Qwen3-Reranker causal-LM backend (deployed by default via compose)
# ---------------------------------------------------------------------------
# bge-reranker-base is a sequence-classification CrossEncoder. Qwen3-Reranker
# is a CAUSAL LM: each (query, doc) pair is formatted with an instruction chat
# template and scored from the next-token logits of the "yes"/"no" tokens.
# It is NOT loadable via sentence_transformers.CrossEncoder, so it gets its
# own backend (_Qwen3CausalReranker) selected by RERANKER_BACKEND=qwen3_causal.
#
# DEPLOYED BY DEFAULT (2026-06-29): docker-compose.yml sets
# RERANKER_BACKEND=qwen3_causal + RERANKER_DEVICE=cuda on the reranker sidecar
# (validated +13.9% NDCG@10 vs bge on the golden bench). Set
# RERANKER_BACKEND=cross_encoder to revert to the CPU bge baseline. A 0.6B
# causal LM doing one forward pass per pair is far slower than bge on CPU and
# will blow RERANKER_TIMEOUT_S — run this backend on GPU.
#
# Score scale (audit 2026-07-01): predict() returns the yes/no next-token
# LOG-ODDS (logit_yes − logit_no) — a sign-preserving real value like a
# CrossEncoder logit — NOT a [0,1] probability. This keeps the
# RERANKER_SCORE_THRESHOLD=0.0 sign filter in search_documents meaningful
# ("net yes-evidence required") and makes the downstream sigmoid produce the
# exact P(yes). See _Qwen3CausalReranker.predict.
RERANKER_BACKEND = (os.environ.get("RERANKER_BACKEND") or "cross_encoder").strip().lower()
QWEN3_RERANKER_MODEL = (
    os.environ.get("QWEN3_RERANKER_MODEL") or "Qwen/Qwen3-Reranker-0.6B"
).strip()
RERANKER_DEVICE = (os.environ.get("RERANKER_DEVICE") or "cpu").strip()
QWEN3_RERANKER_INSTRUCTION = (
    os.environ.get("QWEN3_RERANKER_INSTRUCTION")
    or "Given a geological search query, retrieve relevant passages that answer the query"
)
QWEN3_RERANKER_MAX_LEN = int(os.environ.get("QWEN3_RERANKER_MAX_LEN", "2048"))
QWEN3_RERANKER_BATCH = int(os.environ.get("QWEN3_RERANKER_BATCH", "8"))

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

        from app.sidecar_auth import SERVICE_KEY_HEADERS  # noqa: PLC0415

        payload = {"pairs": [[str(q), str(p)] for q, p in pairs]}
        resp = httpx.post(
            self._url, json=payload, timeout=self._timeout_s,
            headers=SERVICE_KEY_HEADERS,
        )
        resp.raise_for_status()
        return [float(s) for s in resp.json()["scores"]]


class _Qwen3CausalReranker:
    """Qwen3-Reranker (causal-LM) behind the CrossEncoder ``.predict()`` API.

    Mirrors ``CrossEncoder.predict(list[(query, passage)]) -> list[float]`` so it
    is a drop-in for ``get_reranker_or_none()`` consumers. Each pair is scored as
    the yes/no next-token LOG-ODDS (logit_yes − logit_no) from the model's final
    position, per the official Qwen3-Reranker model-card usage. Left-padding
    keeps the final position (-1) aligned to the real last token across a batch.

    Prompt assembly (audit 2026-07-01): the chat-template PREFIX/SUFFIX are
    tokenized once at init and re-attached around the (truncated) query+document
    middle in :meth:`predict` — tokenizing the whole formatted string with
    right-truncation used to cut the SUFFIX off over-length pairs, which moved
    the final position away from the yes/no decision point and produced
    garbage scores for long documents.
    """

    # Chat-template scaffolding from the official Qwen3-Reranker model card.
    _PREFIX = (
        "<|im_start|>system\nJudge whether the Document meets the requirements "
        "based on the Query and the Instruct provided. Note that the answer can "
        'only be "yes" or "no".<|im_end|>\n<|im_start|>user\n'
    )
    _SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

    def __init__(
        self,
        model_name_or_path: str,
        *,
        device: str = "cpu",
        instruction: str = QWEN3_RERANKER_INSTRUCTION,
        max_length: int = QWEN3_RERANKER_MAX_LEN,
        batch_size: int = QWEN3_RERANKER_BATCH,
    ) -> None:
        import torch  # noqa: PLC0415
        from transformers import (  # noqa: PLC0415
            AutoModelForCausalLM,
            AutoTokenizer,
        )

        self._torch = torch
        self._device = device
        self._instruction = instruction
        self._max_length = max_length
        self._batch_size = batch_size

        self._tokenizer = AutoTokenizer.from_pretrained(model_name_or_path)
        # Left padding so the final position (-1) is the real last token for
        # every sequence in a batch (required for next-token scoring).
        self._tokenizer.padding_side = "left"
        self._model = (
            AutoModelForCausalLM.from_pretrained(
                model_name_or_path,
                torch_dtype=(
                    torch.float16 if device.startswith("cuda") else torch.float32
                ),
            )
            .to(device)
            .eval()
        )

        self._token_true = self._tokenizer.convert_tokens_to_ids("yes")
        self._token_false = self._tokenizer.convert_tokens_to_ids("no")
        if self._token_true is None or self._token_false is None:
            raise RuntimeError(
                "Qwen3-Reranker: tokenizer lacks single 'yes'/'no' tokens"
            )

        # Audit 2026-07-01: tokenize the chat scaffold ONCE so predict can
        # truncate ONLY the query+document middle and the SUFFIX (which ends at
        # the yes/no decision position) always survives. Mirrors the official
        # Qwen3-Reranker model-card usage.
        self._prefix_ids: list[int] = self._tokenizer(
            self._PREFIX, add_special_tokens=False
        )["input_ids"]
        self._suffix_ids: list[int] = self._tokenizer(
            self._SUFFIX, add_special_tokens=False
        )["input_ids"]

    def _format_middle(self, query: str, passage: str) -> str:
        """The truncatable middle of the prompt (between PREFIX and SUFFIX)."""
        return (
            f"<Instruct>: {self._instruction}\n"
            f"<Query>: {query}\n<Document>: {passage}"
        )

    def _build_batch_input_ids(
        self, batch: "list[tuple[str, str]]"
    ) -> list[list[int]]:
        """Tokenize a batch: truncate the middle only, re-attach the scaffold.

        Tokenizing the fully-formatted string with ``truncation=True``
        (right-truncation) cut the SUFFIX off over-length pairs — the final
        position then wasn't the yes/no decision point and the scores were
        garbage for long documents (audit 2026-07-01). Instead, the middle is
        truncated to ``max_length − len(prefix) − len(suffix)`` and the
        pre-tokenized prefix/suffix ids are concatenated around it.
        """
        middles = [self._format_middle(str(q), str(p)) for q, p in batch]
        budget = self._max_length - len(self._prefix_ids) - len(self._suffix_ids)
        enc = self._tokenizer(
            middles,
            padding=False,
            truncation="longest_first",
            max_length=budget,
            add_special_tokens=False,
            return_attention_mask=False,
        )
        return [
            self._prefix_ids + ids + self._suffix_ids
            for ids in enc["input_ids"]
        ]

    def predict(self, pairs: "list[tuple[str, str]]") -> list[float]:
        torch = self._torch
        scores: list[float] = []
        with torch.no_grad():
            for start in range(0, len(pairs), self._batch_size):
                batch = pairs[start : start + self._batch_size]
                input_ids = self._build_batch_input_ids(batch)
                # tokenizer.pad honours padding_side="left" (set at init), so
                # position -1 is the real last token for every row.
                enc = self._tokenizer.pad(
                    {"input_ids": input_ids},
                    padding=True,
                    return_tensors="pt",
                ).to(self._device)
                # Next-token logits at the final position; compare yes vs no.
                last_logits = self._model(**enc).logits[:, -1, :]
                # Audit 2026-07-01: return LOG-ODDS (logit_yes − logit_no), not
                # softmax P(yes). Log-odds are sign-preserving like CrossEncoder
                # logits, so (a) the RERANKER_SCORE_THRESHOLD=0.0 sign filter in
                # search_documents keeps meaning "net yes-evidence required"
                # instead of degenerating into a pass-everything gate on [0,1]
                # probabilities, and (b) the downstream sigmoid in tools.py
                # yields the exact P(yes): sigmoid(log-odds) == softmax(
                # [logit_no, logit_yes])[yes]. Log-odds are strictly monotonic
                # in P(yes), so ranking order — and the +13.9% NDCG@10
                # validation of 2026-06-29 — is unchanged.
                log_odds = (
                    last_logits[:, self._token_true].float()
                    - last_logits[:, self._token_false].float()
                )
                scores.extend(log_odds.tolist())
        return scores


@lru_cache(maxsize=1)
def _get_reranker() -> "CrossEncoder | _Qwen3CausalReranker":
    """Load and return the BGE reranker singleton (cached per worker process).

    Raises:
        ImportError: if sentence-transformers is not installed.
        OSError: if the model files cannot be downloaded / found.

    The caller (lifespan hook) should catch and log exceptions -- a reranker
    failure degrades quality but must not prevent service startup.
    """
    global _ACTIVE_VERSION

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

    # Qwen3-Reranker causal-LM backend — the compose default on the reranker
    # sidecar (RERANKER_BACKEND=qwen3_causal, cuda; validated +13.9% NDCG@10 vs
    # bge 2026-06-29). Module-level RERANKER_BACKEND defaults to cross_encoder
    # so in-process loads (tests, eval harness) stay on the bge CrossEncoder
    # below unless explicitly switched.
    if RERANKER_BACKEND == "qwen3_causal":
        model_id = (
            os.environ.get("RERANKER_MODEL_PATH") or ""
        ).strip() or QWEN3_RERANKER_MODEL
        logger.info(
            "Loading Qwen3-Reranker CAUSAL-LM backend: %s device=%s "
            "(scores are yes/no log-odds; slow on CPU — intended for GPU).",
            model_id, RERANKER_DEVICE,
        )
        qwen_reranker = _Qwen3CausalReranker(model_id, device=RERANKER_DEVICE)
        qwen_reranker.predict(
            [("warm up query", "warm up geological document passage")]
        )
        _ACTIVE_VERSION = f"qwen3-causal:{model_id}"
        logger.info("Reranker ready: %s", _ACTIVE_VERSION)
        return qwen_reranker

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
    _ACTIVE_VERSION = active_version
    logger.info("Reranker ready: %s", active_version)
    return model


def get_reranker_or_none() -> "CrossEncoder | _RemoteReranker | _Qwen3CausalReranker | None":
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
