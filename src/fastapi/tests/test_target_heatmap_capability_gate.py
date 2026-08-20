"""The target_heatmap chart must refuse rather than fabricate.

Background (2026-08-20 database review). `_fetch_target_heatmap_cells` needs
three things that only ever existed in `database/raw/`: the `h3` extension,
`h3_postgis`, and `gold.h3_density_mineral`. CD applies migrations and nothing
else, so none of them are on the Azure server.

Before the gate, the missing objects raised UndefinedFunction/UndefinedTable
inside `render`'s real-data block, which catches every fetch failure alike and
serves `body.params` — demo placeholder data — logging only a WARNING. The
caller received a plausible-looking exploration-target heatmap built from
placeholder numbers, with nothing in the response to say so.

This is not fixable by installing the extension. Azure Database for PostgreSQL
Flexible Server does not offer `h3`: it is absent from the server's
`azure.extensions` allowedValues, so `CREATE EXTENSION h3` cannot succeed there
at any privilege level. The capability is permanently missing until the chart
is rewritten without H3, so the gate is the correct long-term behaviour, not a
placeholder.
"""

from __future__ import annotations

import pytest

from app.routers.visualizations import (
    HeatmapCapabilityMissing,
    _fetch_target_heatmap_cells,
    _h3_capability_missing,
)


class _FakeConn:
    """Answers only the two capability probes, in the order the gate asks."""

    def __init__(self, *, has_extension: bool, has_table: bool) -> None:
        self._has_extension = has_extension
        self._has_table = has_table
        self.fetch_called = False

    async def fetchval(self, sql: str, *args: object) -> bool:
        if "pg_extension" in sql:
            return self._has_extension
        if "h3_density_mineral" in sql:
            return self._has_table
        raise AssertionError(f"unexpected probe: {sql}")

    async def fetch(self, *args: object, **kwargs: object) -> list[object]:
        # Reaching here means the gate let a query through to a server that
        # cannot serve it — the exact bug this module exists to prevent.
        self.fetch_called = True
        return []


class _FakeScopedConnection:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_missing_extension_is_reported_not_silently_skipped() -> None:
    conn = _FakeConn(has_extension=False, has_table=True)

    reason = await _h3_capability_missing(conn)

    assert reason is not None
    assert "h3 extension" in reason


@pytest.mark.asyncio
async def test_missing_table_is_reported() -> None:
    conn = _FakeConn(has_extension=True, has_table=False)

    reason = await _h3_capability_missing(conn)

    assert reason is not None
    assert "gold.h3_density_mineral" in reason


@pytest.mark.asyncio
async def test_full_capability_present_returns_no_reason() -> None:
    """The developer cluster has all three, so the gate must be a no-op there."""
    conn = _FakeConn(has_extension=True, has_table=True)

    assert await _h3_capability_missing(conn) is None


@pytest.mark.asyncio
async def test_fetch_raises_before_querying_when_capability_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Azure shape: raise, and do not run the query that would 500."""
    conn = _FakeConn(has_extension=False, has_table=False)
    monkeypatch.setattr(
        "app.routers.visualizations.scoped_connection",
        lambda *a, **k: _FakeScopedConnection(conn),
    )

    with pytest.raises(HeatmapCapabilityMissing) as excinfo:
        await _fetch_target_heatmap_cells(
            pg_pool=object(),
            workspace_id="00000000-0000-0000-0000-000000000001",
            commodity=None,
        )

    assert "h3 extension" in str(excinfo.value)
    assert conn.fetch_called is False, (
        "the gate must short-circuit before the h3 query runs — otherwise the "
        "UndefinedFunction error reaches render's demo fallback and the caller "
        "is served fabricated data"
    )
