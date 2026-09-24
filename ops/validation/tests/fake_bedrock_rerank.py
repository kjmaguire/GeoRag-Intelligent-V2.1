"""A Bedrock-shaped rerank host, so rerank_threshold_probe.py runs without AWS.

WHY THIS EXISTS
    The threshold probe's value depends on two things a unit test of its math
    cannot show: that it scores pairs through the REAL adapter
    (``app.services.reranker._BedrockReranker`` via ``get_reranker_or_none``),
    and that it reports failures (a denied role, scores that depend on batch
    composition) instead of producing a confident number from them. This
    stands in for the two boto3 clients the adapter touches, so the adapter
    code runs unmodified:

      * ``bedrock-agent-runtime``: ``rerank(...)``. Validates the request
        shape the adapter sends against the one ``bedrock_wire.py`` declares
        (queries[0].textQuery.text, INLINE text sources, modelArn,
        numberOfResults == len(sources)). Returns results sorted by score,
        not in input order, as the real API does, so the adapter's
        ``index`` remapping is exercised rather than assumed.
      * ``bedrock``: ``list_foundation_models(byProvider=...)``, for
        ``discover_cohere_rerank_v4_model_id``.

    Scores are a deterministic function of word overlap between the query
    and the document. That is enough for inverse-cloze pairs built from a
    synthetic corpus to separate the way a real reranker's should. It is not
    a model of Rerank 3.5.

MODES (``FAKE_RERANK_MODE``, or the ``mode`` argument)
    separable        (default) overlap-driven scores, on-topic well above
                     off-topic.
    noisy            scores dominated by a hash of the pair: populations
                     overlap and AUC collapses toward 0.5.
    batch_dependent  each score divided by the best in its call. A floor
                     measured here would not transfer to production's
                     40-document calls; the probe's stability check must
                     say so.
    denied           every call raises AccessDeniedException, botocore-shaped.
    with_v4          like ``separable``, but the catalogue also lists a v4
                     model, so discovery switches to it and the probe must
                     report the model it actually used.
"""

from __future__ import annotations

import math
import os
import re
import zlib
from typing import Any

_WORD = re.compile(r"[a-z][a-z0-9\-]{2,}")
_STOP = frozenset(
    ["the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "has", "have", "its", "into", "than"]
)

V35 = "cohere.rerank-v3-5:0"
V4 = "cohere.rerank-v4:0"


class FakeClientError(Exception):
    """Shaped like botocore's ClientError: the probe reads ``.response``."""

    def __init__(self, code: str, message: str, status: int = 403) -> None:
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}}


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def relevance(query: str, document: str, mode: str = "separable") -> float:
    """The fake's scoring rule, exposed so tests can reason about it."""
    q = _words(query)
    overlap = len(q & _words(document)) / max(1, len(q))
    if mode == "noisy":
        jitter = (zlib.crc32(f"{query}\x00{document}".encode()) % 1000) / 1000.0
        return round(0.15 * overlap + 0.6 * jitter + 0.05, 6)
    # A logistic around 30% overlap: steep enough to separate, soft enough
    # that scores are not just 0 and 1.
    return round(1.0 / (1.0 + math.exp(-12.0 * (overlap - 0.3))), 6)


class FakeAgentRuntime:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    def rerank(self, *, queries, sources, rerankingConfiguration):  # noqa: N803 — boto3 kwarg names
        if self.mode == "denied":
            raise FakeClientError("AccessDeniedException", "not authorized to perform bedrock:Rerank")
        assert len(queries) == 1 and queries[0]["type"] == "TEXT", queries
        query = queries[0]["textQuery"]["text"]
        docs = []
        for source in sources:
            assert source["type"] == "INLINE", source
            inline = source["inlineDocumentSource"]
            assert inline["type"] == "TEXT", inline
            docs.append(inline["textDocument"]["text"])
        config = rerankingConfiguration
        assert config["type"] == "BEDROCK_RERANKING_MODEL", config
        bedrock_config = config["bedrockRerankingConfiguration"]
        arn = bedrock_config["modelConfiguration"]["modelArn"]
        assert arn.startswith("arn:aws:bedrock:") and "foundation-model/" in arn, arn
        assert bedrock_config["numberOfResults"] == len(docs), bedrock_config
        self.calls.append({"model_arn": arn, "documents": len(docs)})

        scored = [(i, relevance(query, d, self.mode)) for i, d in enumerate(docs)]
        if self.mode == "batch_dependent":
            best = max(s for _, s in scored) or 1.0
            scored = [(i, round(s / best, 6)) for i, s in scored]
        scored.sort(key=lambda item: item[1], reverse=True)
        return {"results": [{"index": i, "relevanceScore": s} for i, s in scored]}


class FakeControlPlane:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def list_foundation_models(self, **_kwargs):
        if self.mode == "denied":
            raise FakeClientError("AccessDeniedException", "not authorized to perform bedrock:ListFoundationModels")
        ids = ["cohere.embed-v4:0", V35] + ([V4] if self.mode == "with_v4" else [])
        return {"modelSummaries": [{"modelId": m, "providerName": "Cohere"} for m in ids]}


_INSTANCES: dict[tuple[str, str], Any] = {}


def client(service: str, *, mode: str | None = None, **_kwargs: Any) -> Any:
    """Drop-in for ``app.services._bedrock.get_client`` and for ``boto3.client``.

    One instance per (service, mode), so a test can read back ``calls``.
    """
    mode = mode or os.environ.get("FAKE_RERANK_MODE", "separable")
    key = (service, mode)
    if key not in _INSTANCES:
        if service == "bedrock-agent-runtime":
            _INSTANCES[key] = FakeAgentRuntime(mode)
        elif service == "bedrock":
            _INSTANCES[key] = FakeControlPlane(mode)
        else:
            raise AssertionError(f"fake_bedrock_rerank: unexpected service {service!r}")
    return _INSTANCES[key]


def reset() -> None:
    _INSTANCES.clear()
