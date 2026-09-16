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
    """6.0 wrapping two 30s calls must fail startup, not fail silently."""
    with env(TIMEOUT_QDRANT_S="6.0"), pytest.raises(ValidationError) as caught:
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
    "variable", ["BEDROCK_EMBED_TIMEOUT_S", "SPARSE_SERVICE_TIMEOUT_S"]
)
def test_raising_an_inner_budget_past_the_outer_one_is_rejected(variable: str) -> None:
    """The inversion can arrive from either side."""
    with env(**{variable: "600"}), pytest.raises(ValidationError) as caught:
        Settings()

    assert variable in str(caught.value)


def test_the_outer_deadline_is_still_checked() -> None:
    """The original assertion still holds — this extends it, not replaces it."""
    with env(TIMEOUT_GATHER_S="5.0"), pytest.raises(ValidationError) as caught:
        Settings()

    assert "TIMEOUT_GATHER_S=5.0" in str(caught.value)
