"""Regression guard for the 384-vs-1024 embedding dimension landmine (A6).

Background — the shape of the bug this file exists to prevent:

The 2026-06-03 model swap moved the corpus from ``BAAI/bge-small-en-v1.5``
(384-dim) to ``Qwen/Qwen3-Embedding-0.6B`` (1024-dim) and recreated the
``georag_chunks`` Qdrant collection at 1024. The live ``.env`` was updated.
Three things were NOT:

  1. ``docker-compose.yml`` still had ``${EMBEDDING_MODEL_NAME:-BAAI/bge-small-en-v1.5}``
     on the fastapi, embedding-sidecar and hatchet-worker services.
  2. ``app/embedding_service.py`` (the sidecar model host, which deliberately
     avoids importing Settings) had the same 384-dim fallback baked into its
     ``os.environ.get`` default.
  3. ``app/config.py`` meanwhile defaulted ``EMBEDDING_DIMENSION`` to 1024.

So config and code disagreed by construction. Any deploy that lost ``.env`` —
which is the normal condition for a fresh Azure Container Apps revision —
would load a 384-dim model, write 384-dim vectors into a 1024-dim collection,
and fail with an opaque Qdrant HTTP 400 that surfaces to the user as a bare
refusal with no stated cause.

These tests are cheap static assertions on purpose. The failure mode is a
configuration default drifting away from the model, which no runtime test on
a correctly-configured box would ever catch.

The compose-file half of this guard lives in
``scripts/ci/embedding_dimension_check.sh`` rather than here: the FastAPI test
container mounts only ``src/fastapi`` as ``/app``, so a pytest assertion
against ``docker-compose.yml`` would skip silently on every run — a guard that
never executes is worse than no guard, because it reads as coverage.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import settings

# Models whose dense dimension is NOT settings.EMBEDDING_DIMENSION. Extend this
# if the corpus model changes again — the point is that a default naming a
# model of the wrong dimension is always a bug, whatever the current model is.
WRONG_DIMENSION_MODELS = {
    "BAAI/bge-small-en-v1.5": 384,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "BAAI/bge-base-en-v1.5": 768,
}

EXPECTED_MODEL = "Qwen/Qwen3-Embedding-0.6B"
EXPECTED_DIMENSION = 1024


def test_config_dimension_matches_expected_model() -> None:
    """The two halves of app/config.py agree with each other."""
    assert settings.EMBEDDING_DIMENSION == EXPECTED_DIMENSION
    assert settings.EMBEDDING_MODEL_NAME == EXPECTED_MODEL


def test_embedding_sidecar_fallback_matches_config() -> None:
    """app/embedding_service.py's env fallback must match settings.

    This module intentionally does not import Settings (it would drag in
    required secrets the lean model host has no business needing), so nothing
    reconciles the two automatically. That is precisely why it needs pinning.
    """
    import app as _app_pkg

    src = (Path(_app_pkg.__file__).resolve().parent / "embedding_service.py").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r'_MODEL_NAME\s*=\s*os\.environ\.get\(\s*"EMBEDDING_MODEL_NAME"\s*,\s*"([^"]+)"',
        src,
    )
    assert match, (
        "Could not find the _MODEL_NAME env fallback in embedding_service.py. "
        "If it was refactored, re-point this guard rather than deleting it — "
        "the fallback is unreconciled with Settings by design."
    )
    fallback = match.group(1)
    assert fallback not in WRONG_DIMENSION_MODELS, (
        f"embedding_service.py falls back to {fallback!r} "
        f"({WRONG_DIMENSION_MODELS.get(fallback)}-dim) but "
        f"EMBEDDING_DIMENSION={settings.EMBEDDING_DIMENSION}. A sidecar started "
        "without EMBEDDING_MODEL_NAME in its env would serve vectors of the "
        "wrong width to a query path that believes otherwise."
    )
    assert fallback == EXPECTED_MODEL


# ---------------------------------------------------------------------------
# qdrant_dense_dim — the startup collection-parity check's reader
# ---------------------------------------------------------------------------


class _VectorParams:
    """Stand-in for qdrant_client.models.VectorParams (only .size is read)."""

    def __init__(self, size: int) -> None:
        self.size = size


def test_qdrant_dense_dim_reads_named_dict_form() -> None:
    """georag_chunks is the dict form: dense in the unnamed "" slot."""
    from app.main import qdrant_dense_dim

    assert qdrant_dense_dim({"": _VectorParams(1024)}) == 1024


def test_qdrant_dense_dim_reads_bare_params_form() -> None:
    """A single-unnamed-vector collection returns a bare VectorParams."""
    from app.main import qdrant_dense_dim

    assert qdrant_dense_dim(_VectorParams(384)) == 384


def test_qdrant_dense_dim_returns_none_when_dense_slot_absent() -> None:
    """Sparse-only / unexpected shapes yield None, not a false mismatch.

    The startup check treats None as "unknown, don't act" — reporting a
    mismatch here would disable embedding on a healthy collection.
    """
    from app.main import qdrant_dense_dim

    assert qdrant_dense_dim({"text-sparse": _VectorParams(1)}) is None
    assert qdrant_dense_dim(None) is None
    assert qdrant_dense_dim({}) is None
