"""`LLM_BACKEND=vllm` must name an endpoint, and no default may resurrect the
deleted in-cluster `vllm` service.

WHY THIS TEST EXISTS
    The `vllm` compose service was deleted on 2026-07-30 in the Azure AI
    Foundry cutover. `LLM_BACKEND=vllm` deliberately stayed supported for
    operators pointing at an OpenAI-compatible endpoint they run themselves —
    but `VLLM_URL` and `LLM_PRIMARY_URL` kept defaulting to
    `http://vllm:8000/v1`, a hostname that had stopped resolving.

    That combination fails in the least useful way available: selecting the
    backend without setting a URL produces a DNS error naming a service that
    was intentionally removed, which reads as a broken network rather than as
    missing configuration. The defaults are now empty and the validator turns
    the case into a startup error.

    The last assertion is the ratchet: it fails if anyone reintroduces the
    dead hostname as a default.
"""

from __future__ import annotations

import pydantic
import pytest

from app.config import Settings

#: 48 bytes — clears the >= 32-byte HS256 floor `FASTAPI_SERVICE_KEY` enforces.
SERVICE_KEY = "test-only-service-key-not-a-secret-000000000000"


def _settings(**overrides: str) -> Settings:
    """Build Settings from explicit kwargs only.

    `_env_file=None` keeps a developer's local `.env` from supplying a
    `VLLM_URL` and making the negative case pass for the wrong reason.
    """
    return Settings(
        _env_file=None,
        fastapi_service_key=SERVICE_KEY,
        postgres_password="test-only-not-a-secret",
        **overrides,
    )


class TestVllmUrlValidator:
    def test_vllm_backend_without_a_url_is_a_startup_error(self):
        with pytest.raises(pydantic.ValidationError) as exc:
            _settings(llm_backend="vllm", vllm_url="")
        assert "VLLM_URL" in str(exc.value)

    def test_whitespace_is_not_a_url(self):
        """`VLLM_URL="   "` is the shape a half-filled .env produces."""
        with pytest.raises(pydantic.ValidationError):
            _settings(llm_backend="vllm", vllm_url="   ")

    def test_vllm_backend_with_a_url_is_accepted_and_used(self):
        s = _settings(llm_backend="vllm", vllm_url="http://inference.internal:8000/v1")
        assert s.effective_llm_url == "http://inference.internal:8000/v1"

    def test_the_default_azure_backend_needs_no_vllm_url(self):
        """The regression that matters most: the validator must not make the
        default backend harder to configure."""
        s = _settings(azure_foundry_endpoint="https://example.services.ai.azure.com")
        assert s.LLM_BACKEND == "azure"
        assert s.VLLM_URL == ""


def test_no_default_points_at_the_deleted_vllm_service():
    """Ratchet. `http://vllm:8000` resolved only while the compose service
    existed; it was deleted 2026-07-30. A default naming it again would
    reintroduce the confusing failure this module documents."""
    s = _settings()
    for field in ("VLLM_URL", "LLM_PRIMARY_URL"):
        value = getattr(s, field)
        assert "vllm:8000" not in value, (
            f"{field} defaults to {value!r}, which names the `vllm` compose "
            f"service deleted on 2026-07-30. Leave it empty: an operator using "
            f"LLM_BACKEND=vllm supplies their own endpoint."
        )
