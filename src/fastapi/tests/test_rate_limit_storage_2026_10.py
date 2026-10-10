"""Audit 2026-10 finding 18: a Redis rate-limit store is refused, not recommended.

``app/services/rate_limit.py`` and ``config.py`` recommended
``RATE_LIMIT_STORAGE_URI=redis://...`` for staging/prod. slowapi 0.1.x builds its
storage with ``limits.storage.storage_from_string`` and counts hits through the
SYNCHRONOUS ``limits`` strategies, so that URL puts a blocking redis-py call on the
event loop for every rate-limited request (hard rule 2). Nothing in compose, the
Helm chart or Terraform sets the variable, so refusing it at startup costs no
deployment anything.
"""

from __future__ import annotations

from pathlib import Path

import pydantic
import pytest

from app.config import Settings

SERVICE_KEY = "test-only-service-key-not-a-secret-000000000000"


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        FASTAPI_SERVICE_KEY=SERVICE_KEY,
        POSTGRES_PASSWORD="test-only-not-a-secret",
        COHERE_API_KEY="test-only-not-a-real-key",
        **overrides,
    )


@pytest.mark.parametrize(
    "uri",
    [
        "redis://:pw@redis:6379/4",
        "rediss://redis.internal:6380/0",
        "redis+sentinel://sentinel:26379/mymaster",
        "async+redis://redis:6379/4",  # limits' async scheme, which slowapi cannot drive
        "memcached://memcached:11211",
        "REDIS://redis:6379/4",
    ],
)
def test_a_shared_store_is_a_startup_error_that_says_why(uri: str) -> None:
    with pytest.raises(pydantic.ValidationError) as exc:
        _settings(RATE_LIMIT_STORAGE_URI=uri)

    message = str(exc.value)
    assert "RATE_LIMIT_STORAGE_URI" in message
    assert "synchronous" in message
    assert "block the event loop" in message


@pytest.mark.parametrize("uri", [None, "", "   ", "memory://", "MEMORY://", " memory:// "])
def test_unset_or_in_memory_is_accepted(uri: str | None) -> None:
    configured = _settings(RATE_LIMIT_STORAGE_URI=uri)
    assert uri == configured.RATE_LIMIT_STORAGE_URI


def test_the_default_is_unset() -> None:
    assert _settings().RATE_LIMIT_STORAGE_URI is None


def test_the_limiter_the_service_builds_counts_in_memory() -> None:
    from limits.storage import MemoryStorage

    from app.services.rate_limit import limiter

    assert isinstance(limiter._storage, MemoryStorage)


def test_nothing_recommends_a_redis_store_any_more() -> None:
    import app.services.rate_limit as rate_limit_module

    source = Path(rate_limit_module.__file__).read_text()
    assert "Recommended for staging/prod" not in source
    assert "a Redis URL for shared-state across workers" not in source

    # The sample env file used to carry the redis:// line. Repo layout:
    # <repo>/src/fastapi/tests/<this file>.
    env_example = Path(__file__).resolve().parents[3] / ".env.example"
    if env_example.exists():
        text = env_example.read_text()
        assert "RATE_LIMIT_STORAGE_URI=redis" not in text
