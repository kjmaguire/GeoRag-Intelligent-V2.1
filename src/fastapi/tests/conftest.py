"""Shared pytest fixtures for GeoRAG FastAPI integration tests.

This module is auto-loaded by pytest for every test module in the tests/
directory.  It provides:

  - FASTAPI_URL, SERVICE_KEY, TEST_PROJECT_ID constants
  - rag_client fixture: an httpx.AsyncClient pre-configured with auth headers
  - parse_sse_stream(): helper that reads an open streaming response and returns
    the `completed` event payload as a dict

Integration tests in this directory hit the LIVE FastAPI endpoint at
http://localhost:8000.  The full Docker Compose stack must be running before
executing these tests:

    docker compose up -d fastapi postgresql pgbouncer ollama

Run the golden and hallucination suites:
    cd src/fastapi
    python -m pytest tests/test_golden_queries.py tests/test_hallucination_failures.py -v

Suite markers
-------------
  golden        — golden query set (exact answer checks)
  hallucination — adversarial queries that must be refused
  integration   — any test that requires the live stack
"""

from __future__ import annotations

import contextlib
import json
import os
import re

import httpx
import pytest_asyncio

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FASTAPI_URL = "http://localhost:8000"
# R13 — was hardcoded "georag-service-key-dev" (22 bytes, now below the 32B
# floor enforced by Settings). Read from the live env so integration tests
# track whatever the running FastAPI container is using. Falls back to the
# dev default only if FASTAPI_SERVICE_KEY isn't set, and then tests that
# hit /internal/* will 401 — which is the correct signal.
SERVICE_KEY = os.environ.get("FASTAPI_SERVICE_KEY", "georag-service-key-dev")
TEST_PROJECT_ID = "019d74a1-fba8-7165-9ae6-a5bf93eef97d"
TEST_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


def _mint_test_jwt(
    *,
    user_id: str = "1",  # Laravel user ID is bigint; some routers cast to int.
    project_id: str = TEST_PROJECT_ID,
    workspace_id: str = TEST_WORKSPACE_ID,
    ttl_s: int = 3600,
) -> str:
    """Mint a Laravel-equivalent JWT for integration tests.

    Module 9 Chunk 9.4 — extract_user_context now requires Authorization
    on every /v1/* path. Tests get a long-TTL JWT signed with the same
    HS256 secret the running FastAPI uses (FASTAPI_SERVICE_KEY).
    """
    import time as _time

    import jwt as _jwt

    now = int(_time.time())
    payload = {
        "iss": "georag-laravel",
        "aud": "georag-fastapi",
        "sub": user_id,
        "project_id": project_id,
        "workspace_id": workspace_id,
        "roles": ["member"],
        "iat": now,
        "exp": now + ttl_s,
    }
    return _jwt.encode(payload, SERVICE_KEY, algorithm="HS256")


# Standard headers used by all internal endpoint calls.
# Module 9 Chunk 9.4 — Bearer JWT is required on every /v1/* path. The
# test JWT carries the default workspace_id; tests that need a different
# workspace can override via the `headers={...}` kwarg on rag_client.
AUTH_HEADERS = {
    "X-Service-Key": SERVICE_KEY,
    "Authorization": f"Bearer {_mint_test_jwt()}",
    "Content-Type": "application/json",
}
# Backward-compat alias kept so any existing imports of the private name
# don't break before all callers migrate.
_AUTH_HEADERS = AUTH_HEADERS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase H — neo4j_driver fixture for the @pytest.mark.integration tests
# in tests/test_neo4j_drillhole_label.py (and any future graph-side
# integration tests). Connects against NEO4J_HOST + NEO4J_USER +
# NEO4J_PASSWORD env vars. When those aren't set OR the connection
# fails, the fixture calls pytest.skip() so the green-suite signal
# stays honest — these tests demand a live Neo4j with seeded data.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def neo4j_driver():
    """Yield an `AsyncGraphDatabase` driver connected to the configured Neo4j.

    Skips the test when NEO4J_* env vars are unset OR the driver can't
    handshake. Keeps the integration suite functional inside Docker
    Compose (where the env vars + the live container exist) while
    not polluting CI / dev runs that don't have Neo4j up.
    """
    import os as _os

    import pytest as _pytest  # noqa: PLC0415

    try:
        from neo4j import AsyncGraphDatabase  # noqa: PLC0415
    except ImportError:
        _pytest.skip("neo4j driver package not installed")
        return

    host = _os.environ.get("NEO4J_HOST")
    user = _os.environ.get("NEO4J_USER")
    password = _os.environ.get("NEO4J_PASSWORD")
    if not (host and user and password):
        _pytest.skip(
            "neo4j_driver fixture: NEO4J_HOST / NEO4J_USER / NEO4J_PASSWORD "
            "env vars not set — integration test requires live Neo4j"
        )
        return

    driver = AsyncGraphDatabase.driver(
        f"bolt://{host}:7687",
        auth=(user, password),
    )
    try:
        # Handshake — if Neo4j is unreachable, skip rather than ERROR.
        try:
            await driver.verify_connectivity()
        except Exception as exc:  # noqa: BLE001
            await driver.close()
            _pytest.skip(
                f"neo4j_driver fixture: Neo4j unreachable at bolt://{host}:7687"
                f" — {type(exc).__name__}: {exc}"
            )
            return
        yield driver
    finally:
        with contextlib.suppress(Exception):
            await driver.close()


@pytest_asyncio.fixture
async def rag_client() -> httpx.AsyncClient:
    """Yield a pre-configured httpx.AsyncClient for the live FastAPI service.

    Timeout is set to 120 s to accommodate a cold-start Ollama model load on
    the first query of a test run.  Individual test cases may override the
    timeout via the `max_response_time_ms` fixture parameter.
    """
    async with httpx.AsyncClient(
        base_url=FASTAPI_URL,
        headers=_AUTH_HEADERS,
        timeout=120.0,
    ) as client:
        yield client


# ---------------------------------------------------------------------------
# SSE stream parser
# ---------------------------------------------------------------------------


async def parse_sse_stream(response: httpx.Response) -> dict:
    """Parse an open SSE streaming response and return the ``completed`` payload.

    Reads lines from the open stream until a ``completed`` event is found.
    Returns the parsed JSON dict from the ``data:`` line of that event.

    Raises
    ------
    RuntimeError
        If the stream ends with a ``failed`` event (carries the error payload).
    RuntimeError
        If the stream closes without emitting a ``completed`` event (timeout or
        unexpected termination).

    Notes
    -----
    The SSE format used by GeoRAG FastAPI (from Section 07c Option A):

        event: delta
        data: {"token": "..."}

        event: citation
        data: {"citation_id": "[DATA-1]", ...}

        event: completed
        data: {"text": "...", "citations": [...], "confidence": 0.9, ...}
    """
    event_type: str | None = None

    async for raw_line in response.aiter_lines():
        line = raw_line.strip()

        if line.startswith("event:"):
            event_type = line.split(":", 1)[1].strip()

        elif line.startswith("data:"):
            raw_data = line.split(":", 1)[1].strip()
            data = json.loads(raw_data)

            if event_type == "completed":
                return data

            if event_type == "failed":
                raise RuntimeError(f"Query stream emitted failed event: {data}")

    raise RuntimeError("SSE stream ended without a 'completed' event")


# ---------------------------------------------------------------------------
# Helpers used by both test modules
# ---------------------------------------------------------------------------


def assert_no_fabricated_numbers(text: str) -> None:
    """Assert that a response text contains no standalone digit sequences.

    Used to verify that hallucination refusals do not include any made-up
    numerical values (grades, depths, counts, resource estimates, etc.).

    Digit sequences inside brackets (e.g. [DATA-1]) are excluded because they
    are part of the citation marker format, not content numbers.

    Args:
        text: The LLM response text to check.

    Raises:
        AssertionError: If any standalone digit sequence is found.
    """
    # Strip citation markers like [DATA-1], [NI43-2], [PUB-3], [PGEO-4].
    stripped = re.sub(r"\[(?:DATA|NI43|PUB|PGEO)-\d+\]", "", text)
    # Strip drill-hole IDs like PLS-22-08, XLS-24-10, DH-2547 — the digit
    # groups inside a hole ID are structural identifiers, not fabricated
    # content numbers. Pattern matches the Layer-4 entity regex.
    stripped = re.sub(
        r"\b[A-Z]{1,8}-\d{1,6}(?:-\d{1,6})?\b", "", stripped
    )
    # Find remaining digit sequences (integers or decimals).
    numbers = re.findall(r"\b\d+(?:\.\d+)?\b", stripped)
    assert not numbers, (
        f"Hallucination refusal response contains fabricated numbers {numbers!r}: {text!r}"
    )
