"""RERANKER_BACKEND is validated, and the posture log covers every guard
(2026-10-10 audit, findings 10 and 11).

10. Only the literal "bedrock" got fail-closed treatment. Any other value --
    "cohere", a typo -- took the local branch, failed to load a CrossEncoder
    and returned 12 RRF-ordered chunks with no relevance floor: the one
    retrieval-quality gate in the system silently off. The value is now checked
    against the set the code implements and the service refuses to start with
    anything else; and where a value does slip through, anything that is not an
    explicitly local backend is treated as hosted.
11. RETRIEVAL_QUALITY_GATE_ENABLED=false and CHUNK_PROVENANCE_GATE_ENABLED=false
    logged no GEORAG_POSTURE_CRITICAL marker, while Layers 3, 4 and 6 did.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import app.services.reranker as reranker
from app.config import settings
from app.main import POSTURE_CRITICAL_MARKER, _assert_guard_posture, _init_reranker
from app.services._bedrock import RetiredAzureConfiguration
from app.services.reranker import (
    HOSTED_RERANKER_BACKENDS,
    LOCAL_RERANKER_BACKENDS,
    SUPPORTED_RERANKER_BACKENDS,
    UnsupportedRerankerBackend,
    reranker_backend_is_hosted,
    validate_reranker_backend,
)

# ---------------------------------------------------------------------------
# Finding 10 -- the supported set
# ---------------------------------------------------------------------------


def test_the_supported_set_is_what_the_code_implements() -> None:
    """The three values .env.example, docker-compose and Terraform use."""
    assert {"bedrock", "cross_encoder", "qwen3_causal"} == SUPPORTED_RERANKER_BACKENDS
    assert {"bedrock"} == HOSTED_RERANKER_BACKENDS
    assert {"cross_encoder", "qwen3_causal"} == LOCAL_RERANKER_BACKENDS
    assert HOSTED_RERANKER_BACKENDS.isdisjoint(LOCAL_RERANKER_BACKENDS)


@pytest.mark.parametrize("value", sorted(SUPPORTED_RERANKER_BACKENDS))
def test_a_supported_value_passes(value: str) -> None:
    validate_reranker_backend(value)


@pytest.mark.parametrize(
    "value", ["cohere", "bedrok", "cross-encoder", "crossencoder", "qwen3", "local", "none", "", "stub"]
)
def test_anything_else_raises_and_names_the_supported_values(value: str) -> None:
    with pytest.raises(UnsupportedRerankerBackend) as exc_info:
        validate_reranker_backend(value)
    message = str(exc_info.value)
    assert repr(value) in message
    for supported in SUPPORTED_RERANKER_BACKENDS:
        assert supported in message


def test_only_an_explicitly_local_backend_is_not_hosted() -> None:
    assert reranker_backend_is_hosted("bedrock")
    assert not reranker_backend_is_hosted("cross_encoder")
    assert not reranker_backend_is_hosted("qwen3_causal")
    # the point of the audit finding:
    assert reranker_backend_is_hosted("cohere")
    assert reranker_backend_is_hosted("")


# ---------------------------------------------------------------------------
# ...and the loaders refuse an unsupported value instead of degrading
# ---------------------------------------------------------------------------


def test_get_reranker_or_none_raises_rather_than_returning_none(monkeypatch) -> None:
    """None means "no reranker, degrade or fail per backend"; a typo is not
    that. It must not reach the local loader either."""
    monkeypatch.setattr(reranker, "RERANKER_BACKEND", "cohere")

    def _must_not_load() -> None:
        raise AssertionError("a value that is not a backend must not load a local model")

    monkeypatch.setattr(reranker, "_get_reranker", _must_not_load)
    with pytest.raises(UnsupportedRerankerBackend):
        reranker.get_reranker_or_none()


def test_the_retired_values_still_get_their_own_message(monkeypatch) -> None:
    monkeypatch.setattr(reranker, "RERANKER_BACKEND", "foundry")
    with pytest.raises(RetiredAzureConfiguration, match="bedrock"):
        reranker.get_reranker_or_none()


def test_the_sidecar_loader_refuses_a_typo_too(monkeypatch) -> None:
    """reranker_service calls _get_reranker() directly; a typo there used to
    load the default CrossEncoder and say nothing."""
    monkeypatch.setattr(reranker, "RERANKER_BACKEND", "qwen3")
    with pytest.raises(UnsupportedRerankerBackend):
        reranker._get_reranker()


def test_the_lifespan_block_stops_startup_for_an_unsupported_backend(monkeypatch) -> None:
    def boom() -> None:
        raise UnsupportedRerankerBackend("RERANKER_BACKEND='cohere' is not a reranker backend")

    monkeypatch.setattr(reranker, "get_reranker_or_none", boom)
    app = SimpleNamespace(state=SimpleNamespace())
    with pytest.raises(UnsupportedRerankerBackend):
        _init_reranker(app)  # type: ignore[arg-type]


def test_the_lifespan_block_still_swallows_an_ordinary_failure(monkeypatch) -> None:
    def boom() -> None:
        raise RuntimeError("model download failed")

    monkeypatch.setattr(reranker, "get_reranker_or_none", boom)
    app = SimpleNamespace(state=SimpleNamespace())
    _init_reranker(app)  # type: ignore[arg-type]
    assert app.state.reranker is None


# ---------------------------------------------------------------------------
# Finding 11 -- every guard that is switched off pages
# ---------------------------------------------------------------------------

_GUARD_FLAGS = (
    "NUMERICAL_VERIFICATION_ENABLED",
    "ENTITY_RESOLUTION_ENABLED",
    "GEOLOGICAL_CONSTRAINTS_ENABLED",
    "RETRIEVAL_QUALITY_GATE_ENABLED",
    "CHUNK_PROVENANCE_GATE_ENABLED",
)


def _criticals(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "georag.safety" and r.levelno == logging.CRITICAL
    ]


def test_every_guard_on_is_silent(caplog, monkeypatch) -> None:
    for flag in _GUARD_FLAGS:
        monkeypatch.setattr(settings, flag, True)
    with caplog.at_level(logging.CRITICAL, logger="georag.safety"):
        _assert_guard_posture()
    assert _criticals(caplog) == []


@pytest.mark.parametrize("flag", _GUARD_FLAGS)
def test_each_guard_switched_off_logs_the_paging_marker(flag: str, caplog, monkeypatch) -> None:
    for other in _GUARD_FLAGS:
        monkeypatch.setattr(settings, other, True)
    monkeypatch.setattr(settings, flag, False)
    with caplog.at_level(logging.CRITICAL, logger="georag.safety"):
        _assert_guard_posture()
    lines = _criticals(caplog)
    assert len(lines) == 1, lines
    assert lines[0].startswith(POSTURE_CRITICAL_MARKER + " ")
    assert flag in lines[0]


def test_both_restored_gates_off_log_two_lines(caplog, monkeypatch) -> None:
    for flag in _GUARD_FLAGS:
        monkeypatch.setattr(settings, flag, True)
    monkeypatch.setattr(settings, "RETRIEVAL_QUALITY_GATE_ENABLED", False)
    monkeypatch.setattr(settings, "CHUNK_PROVENANCE_GATE_ENABLED", False)
    with caplog.at_level(logging.CRITICAL, logger="georag.safety"):
        _assert_guard_posture()
    lines = _criticals(caplog)
    assert [("Layer 1" in line, "Layer 5" in line) for line in lines] == [(True, False), (False, True)]
