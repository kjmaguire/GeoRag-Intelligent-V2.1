"""_build_project_preamble names the project's CRS from crs_epsg first.

Regression (Red Star, 2026-09-30): the preamble read the deprecated free-text
``crs_datum``, which every project is created with as ``EPSG:32613``, so an
Alaska project created with EPSG:26904 was described to the model as UTM 13N.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.agent.orchestrator import _build_project_preamble


class _Conn:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.sql: list[str] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.sql.append(sql)
        return self.row


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _Pool:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.conn = _Conn(row)

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _preamble(row: dict[str, Any] | None) -> str | None:
    return asyncio.run(_build_project_preamble("01a0f103-0e40-71ab-9eac-38211f414699", _Pool(row)))


def _row(**over: Any) -> dict[str, Any]:
    base = {"project_name": "Red Star", "commodity": None, "crs_epsg": None, "crs_datum": None, "region": None}
    return {**base, **over}


def test_crs_epsg_wins_over_the_32613_datum_default() -> None:
    text = _preamble(_row(crs_epsg=26904, crs_datum="EPSG:32613"))
    assert text is not None
    assert "CRS: EPSG:26904" in text
    assert "32613" not in text


def test_the_datum_is_the_fallback_when_crs_epsg_is_unset() -> None:
    text = _preamble(_row(crs_datum="EPSG:26913"))
    assert text is not None and "CRS: EPSG:26913" in text


@pytest.mark.parametrize("row", [_row(), _row(crs_epsg=0, crs_datum="")])
def test_no_crs_line_when_neither_is_set(row: dict[str, Any]) -> None:
    text = _preamble(row)
    assert text is not None and "CRS:" not in text


def test_the_lookup_selects_crs_epsg() -> None:
    pool = _Pool(_row(crs_epsg=26904))
    asyncio.run(_build_project_preamble("01a0f103-0e40-71ab-9eac-38211f414699", pool))
    assert "crs_epsg" in pool.conn.sql[0]
