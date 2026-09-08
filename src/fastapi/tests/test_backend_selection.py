"""Guard the EMBEDDING_BACKEND / RERANKER_BACKEND bedrock branch selection.

Both app.services.embedding.get_embedding_model() and
app.services.reranker.get_reranker_or_none() branch on a module-level
constant (EMBEDDING_BACKEND / RERANKER_BACKEND) read once from os.environ at
import time — not a pydantic Settings field re-read per call.

Because the flags are frozen at import, monkeypatch.setenv alone does not
exercise the branch — these tests patch the already-bound module attribute
directly (the same thing a fresh process boot with the env var set would
produce), matching the documented "monkeypatchable in tests" contract in
reranker.get_reranker_or_none()'s own docstring.

Rewritten 2026-09-08 for ADR-0022 (Azure AI Foundry → Amazon Bedrock). The
retired-value tests below are the point of the rewrite as much as the
happy-path ones: the failure this whole file exists to prevent is a
deployment whose backend selector still names a host that no longer exists,
and that is now a live possibility for every environment that has not been
repointed.
"""

from __future__ import annotations

import pytest

from app.services import _bedrock, embedding, reranker


@pytest.fixture(autouse=True)
def _no_leftover_foundry_env(monkeypatch):
    """Clear Foundry variables so a developer's own .env cannot fail a test.

    assert_no_retired_foundry_env reads the live environment, so without this
    the happy-path tests would fail on any machine that still has Azure
    credentials exported — which is most of them, right after a migration.
    """
    for name in (
        "AZURE_FOUNDRY_ENDPOINT",
        "AZURE_FOUNDRY_API_KEY",
        "AZURE_FOUNDRY_DEPLOYMENT",
        "AZURE_FOUNDRY_EMBED_DEPLOYMENT",
        "AZURE_FOUNDRY_RERANK_DEPLOYMENT",
        "AZURE_FOUNDRY_PARSE_DEPLOYMENT",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_get_embedding_model_selects_bedrock_when_backend_is_bedrock(monkeypatch) -> None:
    monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "bedrock")
    monkeypatch.setattr(embedding, "BEDROCK_EMBED_MODEL_ID", "cohere.embed-v4:0")

    model = embedding.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")

    assert isinstance(model, embedding._BedrockEmbedding)


def test_get_embedding_model_bedrock_requires_a_model_id(monkeypatch) -> None:
    """An empty model id must fail loudly, not silently fall through to a
    different backend — a half-configured bedrock flag should never look
    like a healthy local-model deployment."""
    monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "bedrock")
    monkeypatch.setattr(embedding, "BEDROCK_EMBED_MODEL_ID", "")

    with pytest.raises(RuntimeError, match="BEDROCK_EMBED_MODEL_ID"):
        embedding.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")


def test_get_reranker_selects_bedrock_when_backend_is_bedrock(monkeypatch) -> None:
    monkeypatch.setattr(reranker, "RERANKER_BACKEND", "bedrock")
    monkeypatch.setattr(reranker, "BEDROCK_RERANK_MODEL_ID", "cohere.rerank-v3-5:0")

    result = reranker.get_reranker_or_none()

    assert isinstance(result, reranker._BedrockReranker)


def test_get_reranker_bedrock_returns_none_when_misconfigured(monkeypatch) -> None:
    """Reranker is optional (RRF-order fallback), so a misconfigured bedrock
    flag degrades to None rather than raising — different contract than
    the embedding model, which is load-bearing and must fail loudly. This
    test pins that intentional asymmetry so it can't regress unnoticed."""
    monkeypatch.setattr(reranker, "RERANKER_BACKEND", "bedrock")
    monkeypatch.setattr(reranker, "BEDROCK_RERANK_MODEL_ID", "")

    assert reranker.get_reranker_or_none() is None


# ---------------------------------------------------------------------------
# Retired Foundry configuration (ADR-0022)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retired", ["foundry", "azure", "FOUNDRY"])
def test_embedding_rejects_retired_backend_values(monkeypatch, retired: str) -> None:
    """A backend selector naming Foundry stops the process.

    Not a fallback: falling through would reach the EMBEDDING_SERVICE_URL
    branch and then the local SentenceTransformer, and on an ECS task
    neither exists — so the query path would come up with no embedding
    model and retrieve nothing while reporting success. That exact failure
    happened in the other direction before 2026-09-06 (ADR-0021 gotcha 1).
    """
    monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", retired)

    with pytest.raises(_bedrock.RetiredAzureConfiguration, match="bedrock"):
        embedding.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")


@pytest.mark.parametrize("retired", ["foundry", "azure"])
def test_reranker_rejects_retired_backend_values(monkeypatch, retired: str) -> None:
    """The reranker raises here even though it returns None for every other
    misconfiguration. Degrading silently to RRF order is right for a
    reranker that is broken by accident and wrong for a deployment that was
    never repointed off a retired host: the first is a bad ordering, the
    second is an operator who does not know they migrated."""
    monkeypatch.setattr(reranker, "RERANKER_BACKEND", retired)

    with pytest.raises(_bedrock.RetiredAzureConfiguration, match="bedrock"):
        reranker.get_reranker_or_none()


def test_bedrock_backend_rejects_leftover_foundry_env(monkeypatch) -> None:
    """Selecting bedrock while Foundry variables are still set is an error.

    This is the shape a half-finished migration actually takes: the backend
    selector gets updated in one place and the credentials are left behind
    everywhere else. The stale variables are harmless to the Bedrock call
    itself, which is exactly why they need to be called out — nothing else
    would ever notice them.
    """
    monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "bedrock")
    monkeypatch.setattr(embedding, "BEDROCK_EMBED_MODEL_ID", "cohere.embed-v4:0")
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://example.services.ai.azure.com")

    with pytest.raises(_bedrock.RetiredAzureConfiguration, match="AZURE_FOUNDRY_ENDPOINT"):
        embedding.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")


# ---------------------------------------------------------------------------
# Import-time defaults
# ---------------------------------------------------------------------------


def _reload_with_env_unset(monkeypatch, module, var: str):
    """Re-evaluate a module's import-time backend constant with ``var`` unset.

    The constants are frozen at import, so the only way to observe the
    *default* is to reload with the variable absent; the module is reloaded
    again afterwards so the test leaves the process exactly as it found it.
    """
    import importlib

    monkeypatch.delenv(var, raising=False)
    try:
        return importlib.reload(module)
    finally:
        # monkeypatch restores the env at teardown, but the module attribute
        # would keep the value computed here; reload once more at teardown
        # so sibling tests see the real process-boot value.
        monkeypatch.undo()
        importlib.reload(module)


def test_embedding_backend_defaults_to_bedrock_when_unset(monkeypatch) -> None:
    """Unset EMBEDDING_BACKEND must select the hosted backend, never a
    self-hosted model host. Production has no GPU host, so a "local" default
    would make an unset variable on an ECS task silently disable retrieval;
    the compose dev stack sets "local" explicitly instead. The default moved
    local -> foundry on 2026-09-06 and foundry -> bedrock on 2026-09-08, for
    the same reason both times."""
    reloaded = _reload_with_env_unset(monkeypatch, embedding, "EMBEDDING_BACKEND")
    assert reloaded.EMBEDDING_BACKEND == "bedrock"


def test_reranker_backend_defaults_to_bedrock_when_unset(monkeypatch) -> None:
    """Same contract for the reranker: unset RERANKER_BACKEND means the
    hosted Cohere reranker, never an in-process CrossEncoder load."""
    reloaded = _reload_with_env_unset(monkeypatch, reranker, "RERANKER_BACKEND")
    assert reloaded.RERANKER_BACKEND == "bedrock"


def test_reranker_version_string_names_the_model_not_the_host() -> None:
    """answer_runs.reranker_version must distinguish v4 from 3.5 runs.

    The Bedrock move drops Rerank v4 to 3.5, and
    RERANKER_SCORE_THRESHOLD_HOSTED still carries the value measured against
    v4. Re-measuring that threshold means comparing answers scored by the two
    versions after the fact, which is only possible if the persisted version
    string carries the model id rather than a host name.
    """
    import os
    from unittest import mock

    with mock.patch.dict(os.environ, {}, clear=False), \
         mock.patch.object(reranker, "RERANKER_BACKEND", "bedrock"), \
         mock.patch.object(reranker, "BEDROCK_RERANK_MODEL_ID", "cohere.rerank-v3-5:0"):
        version = reranker.active_reranker_version()

    assert version == "cohere-bedrock:cohere.rerank-v3-5:0"
    assert "v3-5" in version
