"""A budget must be larger than everything awaited inside it.

Settings._validate_timeout_ordering checks this. It checked one level and
the level below it was inverted, which is how the bug this file guards
reached a deploy.

TIMEOUT_QDRANT_S reads like "how long to wait for Qdrant". It is not.
tools.py::_run_search gathers the embedding call and the sparse encode
first and queries Qdrant with their results, so the budget wraps all three.
On AWS the first two are network calls with their own 30s budgets --
Bedrock Embed v4, and an HTTP hop to a SPLADE++ sidecar on half a vCPU --
and TIMEOUT_QDRANT_S was 6.0.

What made it dangerous rather than merely wrong is the failure shape. The
outer wait_for does not raise through; tools.py catches TimeoutError and
returns an EMPTY DocumentSearchResult with "(timeout)" in data_source.
Downstream that is indistinguishable from "nothing matched", so retrieval
returned nothing and the pipeline carried on as though the corpus were
empty. The nightly EventBridge shutdown makes every morning's first query
a cold one, so this was the daily path, not an edge case.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest
from pydantic import ValidationError

from app.config import Settings


@contextmanager
def env(**overrides: str):
    """Set env vars for the block, restoring whatever was there before."""
    previous = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, was in previous.items():
            if was is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = was


def test_the_shipped_defaults_nest_correctly() -> None:
    settings = Settings()

    embed = float(os.environ.get("BEDROCK_EMBED_TIMEOUT_S", "30") or "30")
    sparse = float(os.environ.get("SPARSE_SERVICE_TIMEOUT_S", "30") or "30")

    assert embed < settings.TIMEOUT_QDRANT_S, (
        f"TIMEOUT_QDRANT_S={settings.TIMEOUT_QDRANT_S} wraps a "
        f"{embed}s embedding call"
    )
    assert sparse < settings.TIMEOUT_QDRANT_S, (
        f"TIMEOUT_QDRANT_S={settings.TIMEOUT_QDRANT_S} wraps a "
        f"{sparse}s sparse encode"
    )
    assert settings.TIMEOUT_GATHER_S > settings.TIMEOUT_QDRANT_S
    assert settings.TIMEOUT_GATHER_S > settings.TIMEOUT_RERANKER_S


def test_the_value_that_shipped_is_now_rejected() -> None:
    """6.0 wrapping two 30s calls must fail startup, not fail silently.

    Modelled on production, where both inner budgets are live:
    EMBEDDING_BACKEND is bedrock and SPARSE_SERVICE_URL points at the
    sidecar (deploy/aws/terraform/config.tf sets both). That is the
    environment 6.0 shipped into.
    """
    with env(
        TIMEOUT_QDRANT_S="6.0",
        EMBEDDING_BACKEND="bedrock",
        SPARSE_SERVICE_URL="http://sparse.georag.local:8000",
    ), pytest.raises(ValidationError) as caught:
        Settings()

    message = str(caught.value)
    assert "TIMEOUT_QDRANT_S=6.0" in message
    # The message has to name what is actually inside the budget, or the
    # operator raises TIMEOUT_GATHER_S -- the wrong knob -- and nothing changes.
    assert "BEDROCK_EMBED_TIMEOUT_S" in message
    assert "SPARSE_SERVICE_TIMEOUT_S" in message
    # And it has to say why a too-small budget here is worse than an error.
    assert "EMPTY" in message


@pytest.mark.parametrize(
    ("variable", "activating"),
    [
        # Each inner budget is only checked when it is actually in the code
        # path, so each case has to select that path first.
        ("BEDROCK_EMBED_TIMEOUT_S", {"EMBEDDING_BACKEND": "bedrock"}),
        ("SPARSE_SERVICE_TIMEOUT_S", {"SPARSE_SERVICE_URL": "http://sparse:8000"}),
    ],
)
def test_raising_an_inner_budget_past_the_outer_one_is_rejected(
    variable: str, activating: dict[str, str]
) -> None:
    """The inversion can arrive from either side."""
    with env(**{variable: "600"}, **activating), pytest.raises(ValidationError) as caught:
        Settings()

    assert variable in str(caught.value)


def test_a_budget_no_call_reaches_is_not_checked() -> None:
    """Scoping is the difference between a useful check and a blocked deploy.

    The E2E smoke job runs a stubbed local embedder and an in-process sparse
    encoder, so neither 30s budget is reachable there. An unconditional check
    refused to construct Settings at all in that environment -- which is not
    a stricter check, it is a check that stops the app booting over two
    numbers nothing was ever going to read.
    """
    # Local embedder: the Bedrock budget is unreachable, however large.
    with env(EMBEDDING_BACKEND="local", BEDROCK_EMBED_TIMEOUT_S="600"):
        Settings()

    # No sidecar URL: encode_sparse runs in-process and never consults the
    # HTTP client timeout.
    previous = os.environ.pop("SPARSE_SERVICE_URL", None)
    try:
        with env(SPARSE_SERVICE_TIMEOUT_S="600"):
            Settings()
    finally:
        if previous is not None:
            os.environ["SPARSE_SERVICE_URL"] = previous


def test_the_e2e_smoke_environment_boots() -> None:
    """The exact env of the CI job my first version of this check broke."""
    with env(
        EMBEDDING_BACKEND="local",
        TIMEOUT_GATHER_S="25",
        TIMEOUT_RERANKER_S="22",
        TIMEOUT_QDRANT_S="20",
    ):
        settings = Settings()

    assert settings.TIMEOUT_QDRANT_S == 20.0


def test_the_outer_deadline_is_still_checked() -> None:
    """The original assertion still holds — this extends it, not replaces it."""
    with env(TIMEOUT_GATHER_S="5.0"), pytest.raises(ValidationError) as caught:
        Settings()

    assert "TIMEOUT_GATHER_S=5.0" in str(caught.value)
