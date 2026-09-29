"""Why a public-geo fetch came back short.

The ArcGIS fetcher degrades to "no more features" rather than raising,
because one dead survey must not end a sync of thirty others. Before this
existed that degradation was also silent: ``arcgis._get_json`` swallowed the
exception, returned None, and the feed's stats read ``fetched=0`` with
nothing to say whether the service was down, 403ing, answering with an
in-band ArcGIS error, or genuinely empty.

A ``FetchReport`` is the sink for that reason. The bulk walker takes one,
record the first failure into it, and ``sync.sync_source`` copies it into the
per-feed stats (and from there into the audit row and the Hatchet output), so
the ``error`` field says *why* next to the zero.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass
class FetchReport:
    """Mutable per-feed fetch outcome. One instance per feed per run."""

    #: Human-readable reason, e.g. ``"HTTP 403 Forbidden"`` or
    #: ``"ArcGIS error 400: Invalid query parameters"``. None = no failure.
    error: str | None = None
    #: Machine-readable class: ``http_status`` | ``transport`` |
    #: ``invalid_json`` | ``arcgis_error``.
    error_kind: str | None = None
    #: Upstream HTTP status when one was received.
    http_status: int | None = None
    #: Pages successfully received before the walk ended.
    pages: int = 0

    def fail(self, kind: str, message: str, *, http_status: int | None = None) -> None:
        """Record a failure. The FIRST one wins — later ones are consequences."""
        if self.error is not None:
            return
        self.error_kind = kind
        self.error = message[:400]
        self.http_status = http_status

    def as_stats(self) -> dict[str, object]:
        """The fields merged into a feed's stats dict (empty when clean)."""
        if self.error is None:
            return {}
        out: dict[str, object] = {"error": self.error, "error_kind": self.error_kind}
        if self.http_status is not None:
            out["http_status"] = self.http_status
        return out


def make_client(timeout_s: float) -> httpx.AsyncClient:
    """The one place public-geo fetchers build an HTTP client.

    Async-native (CLAUDE.md hard rule 2) — the previous ArcGIS path ran a
    synchronous ``httpx.get`` in a worker thread. Tests replace this function
    with one returning a client over ``httpx.MockTransport``, so no fetcher
    test ever touches the network.
    """
    return httpx.AsyncClient(
        timeout=timeout_s,
        follow_redirects=True,
        headers={"User-Agent": "GeoRAG-public-geo-sync/1.0"},
    )


def describe_exception(exc: BaseException) -> str:
    """``"ConnectTimeout: timed out"`` — type first, so a blank message still says something."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
