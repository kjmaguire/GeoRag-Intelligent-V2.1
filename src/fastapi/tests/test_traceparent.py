"""Module 10 Chunk 10.6 — W3C Trace Context round-trip tests.

Asserts that StructuredAccessLogMiddleware:
  1. Accepts a valid inbound `traceparent` and forwards it on the response.
  2. Mints a fresh `traceparent` when the inbound header is missing.
  3. Mints a fresh `traceparent` when the inbound header is malformed.
  4. Exposes the parsed trace_id on `request.state.trace_id`.

These run in the fast suite (no live stack required) — uses Starlette's
TestClient against a small ASGI app wrapping the middleware.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.middleware import (
    StructuredAccessLogMiddleware,
    _is_valid_traceparent,
    _mint_traceparent,
)


def _make_app() -> TestClient:
    app = FastAPI()
    app.add_middleware(StructuredAccessLogMiddleware)

    @app.get("/_trace_echo")
    async def echo(request: Request) -> dict[str, str]:
        return {
            "traceparent": getattr(request.state, "traceparent", "none"),
            "trace_id": getattr(request.state, "trace_id", "none"),
        }

    return TestClient(app)


def test_inbound_valid_traceparent_is_forwarded() -> None:
    client = _make_app()
    incoming = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"

    resp = client.get("/_trace_echo", headers={"traceparent": incoming})

    assert resp.status_code == 200
    assert resp.headers["traceparent"] == incoming
    assert resp.json()["traceparent"] == incoming
    assert resp.json()["trace_id"] == "0123456789abcdef0123456789abcdef"


def test_missing_traceparent_is_minted() -> None:
    client = _make_app()
    resp = client.get("/_trace_echo")

    assert resp.status_code == 200
    minted = resp.headers["traceparent"]
    assert _is_valid_traceparent(minted)
    # Trace-id slice is 32 hex chars at positions 3..35.
    assert resp.json()["trace_id"] == minted[3:35]


def test_malformed_traceparent_is_replaced() -> None:
    client = _make_app()
    bad = "not-a-valid-traceparent"

    resp = client.get("/_trace_echo", headers={"traceparent": bad})

    minted = resp.headers["traceparent"]
    assert _is_valid_traceparent(minted), f"replacement {minted!r} not valid"
    assert minted != bad


def test_unsampled_traceparent_is_accepted_as_is() -> None:
    """Flags=00 means parent didn't sample — we still propagate verbatim.

    Sampling decisions are made elsewhere; the trace-id must be preserved
    so distributed traces stitch together regardless of flags.
    """
    client = _make_app()
    unsampled = "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-00"
    resp = client.get("/_trace_echo", headers={"traceparent": unsampled})

    assert resp.headers["traceparent"] == unsampled


def test_validator_rejects_wrong_version() -> None:
    """Future versions ("01-...", "02-...") are reserved; v00 is canonical."""
    assert not _is_valid_traceparent(
        "01-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    )


def test_validator_rejects_uppercase_hex() -> None:
    """Spec requires lowercase hex."""
    assert not _is_valid_traceparent(
        "00-0123456789ABCDEF0123456789ABCDEF-0123456789ABCDEF-01"
    )


def test_minted_traceparents_are_unique() -> None:
    """Cryptographic uniqueness — collision over 100 mints is ~0."""
    minted = {_mint_traceparent() for _ in range(100)}
    assert len(minted) == 100
