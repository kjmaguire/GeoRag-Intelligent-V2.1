"""Guard the EMBEDDING_BACKEND / RERANKER_BACKEND foundry branch selection.

Both app.services.embedding.get_embedding_model() and
app.services.reranker.get_reranker_or_none() branch on a module-level
constant (EMBEDDING_BACKEND / RERANKER_BACKEND) read once from os.environ at
import time — not a pydantic Settings field re-read per call. No existing
test set either flag to "foundry" and asserted the correct backend class
came back; test_foundry_retry.py only covers the generic HTTP retry wrapper
both Foundry classes share, not backend selection itself.

Because the flags are frozen at import, monkeypatch.setenv alone does not
exercise the branch — these tests patch the already-bound module attribute
directly (the same thing a fresh process boot with the env var set would
produce), matching the documented "monkeypatchable in tests" contract in
reranker.get_reranker_or_none()'s own docstring.
"""

from __future__ import annotations

import pytest

from app.services import embedding, reranker


def test_get_embedding_model_selects_foundry_when_backend_is_foundry(monkeypatch) -> None:
    monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "foundry")
    monkeypatch.setattr(embedding, "AZURE_FOUNDRY_EMBED_DEPLOYMENT", "embed-v-4-0")
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://example-foundry.cognitiveservices.azure.com")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "fake-key-for-test")

    model = embedding.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")

    assert isinstance(model, embedding._FoundryEmbedding)


def test_get_embedding_model_foundry_requires_full_config(monkeypatch) -> None:
    """Partial Foundry config must fail loudly, not silently fall through
    to a different backend — a half-configured foundry flag should never
    look like a healthy local-model deployment."""
    monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "foundry")
    monkeypatch.setattr(embedding, "AZURE_FOUNDRY_EMBED_DEPLOYMENT", "")
    monkeypatch.delenv("AZURE_FOUNDRY_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="EMBEDDING_BACKEND=foundry"):
        embedding.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")


def test_get_reranker_selects_foundry_when_backend_is_foundry(monkeypatch) -> None:
    monkeypatch.setattr(reranker, "RERANKER_BACKEND", "foundry")
    monkeypatch.setattr(reranker, "AZURE_FOUNDRY_RERANK_DEPLOYMENT", "Cohere-rerank-v4.0-pro")
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://example-foundry.cognitiveservices.azure.com")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "fake-key-for-test")

    result = reranker.get_reranker_or_none()

    assert isinstance(result, reranker._FoundryReranker)


def test_get_reranker_foundry_returns_none_when_misconfigured(monkeypatch) -> None:
    """Reranker is optional (RRF-order fallback), so a misconfigured foundry
    flag degrades to None rather than raising — different contract than
    the embedding model, which is load-bearing and must fail loudly. This
    test pins that intentional asymmetry so it can't regress unnoticed."""
    monkeypatch.setattr(reranker, "RERANKER_BACKEND", "foundry")
    monkeypatch.setattr(reranker, "AZURE_FOUNDRY_RERANK_DEPLOYMENT", "")
    monkeypatch.delenv("AZURE_FOUNDRY_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_FOUNDRY_API_KEY", raising=False)

    assert reranker.get_reranker_or_none() is None
