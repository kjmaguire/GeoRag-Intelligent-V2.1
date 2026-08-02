"""Cross-encoder reranker singleton for GeoRAG hybrid retrieval.

Module 4 Phase B Chunk 3 -- B6 reranker wiring.

Model
-----
Qwen/Qwen3-Reranker-0.6B (Apache 2.0, ~1.2 GB).
Swapped 2026-06-03 from BAAI/bge-reranker-base after the §41 LoRA-on-bge
exploration was parked (memory: project_reranker_overnight_2026_05_29 —
THREE HOLD verdicts, all FT runs failed to beat stock). Qwen3-Reranker
is architecturally a CausalLM that returns a yes/no logit ratio, but
sentence-transformers `CrossEncoder` wraps it transparently — the
.predict(pairs) call site is unchanged.

Why the swap:
  - MTEB-retrieval reranker leaderboard: Qwen3-Reranker-0.6B beats
    bge-reranker-base by ~5-10pp NDCG@10 on average across BEIR
    out-of-the-box, before any GeoRAG-specific tuning.
  - Native 32K context vs bge-reranker-base's 512-token cap. The
    pre-truncation to ~2000 chars in tools.search_documents can be
    relaxed (still kept as a CPU-budget knob, not a model constraint).
  - Family-aligned with the Qwen3-14B-AWQ synthesizer — shared
    tokenizer family + training data distribution.

Revision pinning
----------------
We pin by HF revision SHA to prevent silent weight drift, same as the
bge era. When the model is updated upstream, update RERANKER_REVISION
and RERANKER_VERSION together. The version string is persisted to
answer_runs.reranker_version so any shift in reranker behaviour is
traceable via the audit trail.

Score scale
-----------
Qwen3-Reranker emits a yes-token logit minus a no-token logit. Raw
output is unbounded real ([-15, +15] typical range, broader than bge's
[-10, +10]). The orchestrator's RERANKER_SCORE_THRESHOLD semantic
("0.0 = any positive logit means relevant") carries over, but operators
should re-tune the threshold against golden_queries after the swap —
the absolute magnitudes are different even though the sign convention
matches.

CPU performance
---------------
0.6B params vs bge's 278M ≈ 2× the per-pair latency on CPU
(~400-500 ms/pair at torch_threads=10 on AVX2; bge was ~200-250 ms).
RERANKER_INPUT_CHAR_BUDGET stays at 2000 to keep batch latency under
the 8 s TIMEOUT_RERANKER_S budget. GPU path (when present) is ~10×
faster.

Version string
--------------
RERANKER_VERSION = "qwen3-reranker-0.6b@<first 8 chars of SHA>"
Used by the orchestrator to populate answer_runs.reranker_version.
Operators querying the audit trail can diff the pre-swap rows
(`bge-reranker-base@*`) against post-swap rows for the same query.

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

RERANKER_MODEL_NAME = "Qwen/Qwen3-Reranker-0.6B"
# Pin the known model to an immutable Hub commit. Operators changing the
# model must deliberately update RERANKER_REVISION alongside it.
import os as _os_pin  # noqa: E402, PLC0415

RERANKER_REVISION = _os_pin.environ.get(
    "RERANKER_REVISION",
    "e61197ed45024b0ed8a2d74b80b4d909f1255473",
)
RERANKER_VERSION = f"qwen3-reranker-0.6b@{RERANKER_REVISION[:8]}"

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
# Qwen3-Reranker causal-LM backend (audit 2026-06-28, OPT-IN, NOT deployed)
# ---------------------------------------------------------------------------
# bge-reranker-base is a sequence-classification CrossEncoder. Qwen3-Reranker
# is a CAUSAL LM: each (query, doc) pair is formatted with an instruction chat
# template and scored from the next-token logits of the "yes"/"no" tokens
# (relevance = softmax([no, yes])[yes]). It is NOT loadable via
# sentence_transformers.CrossEncoder, so it gets its own backend selected by
# RERANKER_BACKEND=qwen3_causal. Default stays "cross_encoder" (bge) — this code
# path does nothing until explicitly enabled.
#
# ⚠️ NOT DEPLOYED: a 0.6B causal LM doing one forward pass per pair is far
# slower than bge on CPU and will blow RERANKER_TIMEOUT_S. Run on GPU
# (RERANKER_DEVICE=cuda, needs VRAM headroom) and validate against the golden
# eval before enabling. See manual Ch18 §2 reranker note.
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
# Azure AI Foundry (Cohere Rerank v4) backend — RERANKER_BACKEND=foundry
# ---------------------------------------------------------------------------
# Selected alongside the existing cross_encoder/qwen3_causal values. Unlike
# those two, this path never loads a local model at all — no torch, no
# sentence_transformers, no CPU thread tuning. Reuses the same Azure AI
# Services resource as the LLM (AZURE_FOUNDRY_ENDPOINT/API_KEY), with its own
# deployment name since rerank is deployed as a separate model on that
# resource. Read via os.environ (not app.config.settings) to match this
# module's existing convention of reading env vars directly.
AZURE_FOUNDRY_RERANK_DEPLOYMENT = (
    os.environ.get("AZURE_FOUNDRY_RERANK_DEPLOYMENT") or ""
).strip()
AZURE_FOUNDRY_RERANK_TIMEOUT_S = float(
    os.environ.get("AZURE_FOUNDRY_RERANK_TIMEOUT_S", "8.0")
)


def active_reranker_version() -> str:
    """Return the version string to persist to answer_runs.reranker_version.

    RERANKER_VERSION is a module-level constant fixed to the Qwen3 identity —
    accurate for the cross_encoder (default) backend, but wrong when
    RERANKER_BACKEND=foundry. Callers that need an audit-trail-accurate
    label (main.py's lifespan hook) should use this instead of the raw
    constant.
    """
    if RERANKER_BACKEND == "foundry":
        return f"cohere-foundry:{AZURE_FOUNDRY_RERANK_DEPLOYMENT or 'unset'}"
    return RERANKER_VERSION

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

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        import httpx  # noqa: PLC0415

        from app.sidecar_auth import SERVICE_KEY_HEADERS  # noqa: PLC0415

        payload = {"pairs": [[str(q), str(p)] for q, p in pairs]}
        resp = httpx.post(
            self._url, json=payload, timeout=self._timeout_s,
            headers=SERVICE_KEY_HEADERS,
        )
        resp.raise_for_status()
        return [float(s) for s in resp.json()["scores"]]


class _FoundryReranker:
    """Cohere Rerank v4 (Azure AI Foundry) behind the CrossEncoder ``.predict()`` API.

    Mirrors ``CrossEncoder.predict(list[(query, passage)]) -> list[float]`` so
    it is a drop-in for ``get_reranker_or_none()`` consumers, same contract as
    ``_RemoteReranker``/``_Qwen3CausalReranker`` above.

    Wire shape empirically verified 2026-07-30 against a live deployment
    (Cohere's own custom rerank API, proxied through Azure — NOT the unified
    chat-completions/Model-Inference-API surface):
        POST {endpoint}/providers/cohere/v2/rerank
        api-key: <key>
        body: {"model": "<deployment>", "query": str,
               "documents": [str, ...], "top_n": int}
        -> {"results": [{"index": int, "relevance_score": float}, ...]}

    Cohere's rerank API takes ONE query + N documents per call — a real
    shape difference from the pairwise CrossEncoder contract callers use.
    ``predict`` groups the incoming pairs by their shared query (in practice
    every call site passes pairs sharing a single query, but this groups
    defensively rather than assume), issues one rerank call per group with
    ``top_n`` set to the full document count so every candidate gets scored
    back (not just Cohere's own top-N), and remaps scores to the caller's
    original pair order via the returned ``index`` field.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str,
        timeout_s: float,
    ) -> None:
        self._url = endpoint.rstrip("/") + "/providers/cohere/v2/rerank"
        self._api_key = api_key
        self._deployment = deployment
        self._timeout_s = timeout_s

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        import httpx  # noqa: PLC0415

        from app.services._foundry_retry import with_foundry_retry  # noqa: PLC0415

        groups: dict[str, list[int]] = {}
        for i, (query, _passage) in enumerate(pairs):
            groups.setdefault(str(query), []).append(i)

        scores: list[float] = [0.0] * len(pairs)
        with httpx.Client(timeout=self._timeout_s) as client:
            for query, indices in groups.items():
                documents = [str(pairs[i][1]) for i in indices]

                def _do(query: str = query, documents: list[str] = documents) -> httpx.Response:
                    return client.post(
                        self._url,
                        headers={"api-key": self._api_key},
                        json={
                            "model": self._deployment,
                            "query": query,
                            "documents": documents,
                            "top_n": len(documents),
                        },
                    )

                resp = with_foundry_retry(_do, label="foundry_rerank")
                for result in resp.json()["results"]:
                    scores[indices[result["index"]]] = float(result["relevance_score"])
        return scores


class _Qwen3CausalReranker:
    """Qwen3-Reranker (causal-LM) behind the CrossEncoder ``.predict()`` API.

    Mirrors ``CrossEncoder.predict(list[(query, passage)]) -> list[float]`` so it
    is a drop-in for ``get_reranker_or_none()`` consumers. Each pair is scored as
    P(yes) from the model's next-token logits over the "yes"/"no" tokens, per the
    official Qwen3-Reranker model-card usage. Left-padding keeps the final
    position (-1) aligned to the real last token across a batch.
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

        self._tokenizer = AutoTokenizer.from_pretrained(
            model_name_or_path,
            revision=RERANKER_REVISION,
            trust_remote_code=False,
        )
        # Left padding so the final position (-1) is the real last token for
        # every sequence in a batch (required for next-token scoring).
        self._tokenizer.padding_side = "left"
        self._model = (
            AutoModelForCausalLM.from_pretrained(
                model_name_or_path,
                revision=RERANKER_REVISION,
                trust_remote_code=False,
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

    def _format(self, query: str, passage: str) -> str:
        return (
            f"{self._PREFIX}<Instruct>: {self._instruction}\n"
            f"<Query>: {query}\n<Document>: {passage}{self._SUFFIX}"
        )

    def predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        torch = self._torch
        scores: list[float] = []
        with torch.no_grad():
            for start in range(0, len(pairs), self._batch_size):
                batch = pairs[start : start + self._batch_size]
                texts = [self._format(str(q), str(p)) for q, p in batch]
                enc = self._tokenizer(
                    texts,
                    padding=True,
                    truncation=True,
                    max_length=self._max_length,
                    return_tensors="pt",
                ).to(self._device)
                # Next-token logits at the final position; compare yes vs no.
                last_logits = self._model(**enc).logits[:, -1, :]
                pair_logits = torch.stack(
                    [last_logits[:, self._token_false], last_logits[:, self._token_true]],
                    dim=1,
                )
                probs = torch.softmax(pair_logits.float(), dim=1)
                scores.extend(probs[:, 1].tolist())
        return scores


@lru_cache(maxsize=1)
def _get_reranker() -> CrossEncoder | _Qwen3CausalReranker:
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
    try:  # noqa: SIM105
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

    # Audit 2026-06-28 — opt-in Qwen3-Reranker causal-LM backend. Default
    # (RERANKER_BACKEND=cross_encoder) skips this and loads the bge CrossEncoder
    # below; this path only runs when explicitly enabled.
    if RERANKER_BACKEND == "qwen3_causal":
        model_id = (
            os.environ.get("RERANKER_MODEL_PATH") or ""
        ).strip() or QWEN3_RERANKER_MODEL
        logger.warning(
            "Loading Qwen3-Reranker CAUSAL-LM backend: %s device=%s. NOTE: slow "
            "on CPU — intended for GPU + golden-eval validation, not yet a "
            "validated production swap.",
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
            trust_remote_code=False,
            device="cpu",
        )
        active_version = RERANKER_VERSION

    # Warm-up pass so the first real query doesn't pay JIT compilation cost.
    model.predict([("warm up query", "warm up geological document passage")])
    _ACTIVE_VERSION = active_version
    logger.info("Reranker ready: %s", active_version)
    return model


def get_reranker_or_none() -> (
    CrossEncoder | _RemoteReranker | _Qwen3CausalReranker | _FoundryReranker | None
):
    """Return the reranker (Foundry client, local singleton, remote proxy, or None).

    Precedence: RERANKER_BACKEND=foundry short-circuits before anything else
    — no torch, no sentence_transformers, no local model load at all. Then
    RERANKER_SERVICE_URL (HTTP proxy to the shared `reranker` sidecar).
    Otherwise loads the in-process CrossEncoder singleton. All exceptions are
    caught so callers can handle the absent-reranker path (RRF order
    fallback) without try/except boilerplate. Env is read fresh each call so
    it stays monkeypatchable in tests.
    """
    if RERANKER_BACKEND == "foundry":
        endpoint = (os.environ.get("AZURE_FOUNDRY_ENDPOINT") or "").strip()
        api_key = (os.environ.get("AZURE_FOUNDRY_API_KEY") or "").strip()
        if not (endpoint and api_key and AZURE_FOUNDRY_RERANK_DEPLOYMENT):
            logger.error(
                "reranker: RERANKER_BACKEND=foundry but AZURE_FOUNDRY_ENDPOINT/"
                "API_KEY/AZURE_FOUNDRY_RERANK_DEPLOYMENT not fully set -- "
                "rerank step will be skipped"
            )
            return None
        return _FoundryReranker(
            endpoint, api_key, AZURE_FOUNDRY_RERANK_DEPLOYMENT,
            AZURE_FOUNDRY_RERANK_TIMEOUT_S,
        )
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
