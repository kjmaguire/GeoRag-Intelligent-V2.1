"""The long section keeps a 0-degree azimuth and says when it cut the holes.

GIS audit 2026-10, finding 12. ``_fetch_long_section_collars`` ended with
``reference_azimuth_deg or 90.0``, and 0.0 is falsy: a request for a
north-south section (azimuth 0) came back as an east-west one (90), drawn and
labelled as if it were what was asked for. It also took the first 100 holes by
``hole_id`` and said nothing, so a 312-hole project was drawn as a 100-hole one.

No Postgres here (the query runs in tests/test_viz_long_section_pg.py): the
connection is scripted.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.routers.visualizations as viz
from app.services.auth import UserContext, extract_user_context, verify_service_key
from app.services.visualizations.additional_charts import long_section_figure
from tests._stub_pool import StubConn

WS = UUID("b0000000-0000-0000-0000-00000000000a")
PROJECT = UUID("019d74a1-fba8-7165-9ae6-a5bf93eef97d")


def _row(i: int, total: int) -> dict[str, Any]:
    return {
        "hole_id": f"DDH{i:03d}",
        "easting": 500_000.0 + 50.0 * i,
        "northing": 6_460_000.0,
        "elevation": 480.0,
        "total_depth": 150.0,
        "azimuth": 90.0,
        "inclination": -60.0,
        "holes_total": total,
    }


class _Scope:
    """Replaces ``scoped_connection``; hands back a connection that answers the one query."""

    def __init__(self, shown: int, total: int) -> None:
        self.conn = StubConn(fetch=[("silver.collars", [_row(i, total) for i in range(shown)])])

    def __call__(self, pool: Any, *, workspace_id: str, site: str = "unknown", **_: Any):  # type: ignore[no-untyped-def]
        return self._scope()

    @asynccontextmanager
    async def _scope(self):  # type: ignore[no-untyped-def]
        yield self.conn


async def _fetch(monkeypatch: pytest.MonkeyPatch, *, shown: int, total: int, azimuth: float | None) -> dict[str, Any]:
    monkeypatch.setattr(viz, "scoped_connection", _Scope(shown, total))
    return await viz._fetch_long_section_collars(
        pg_pool=object(), workspace_id=str(WS), project_id=PROJECT, reference_azimuth_deg=azimuth
    )


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(viz.router)
    app.state.pg_pool = object()
    app.state.redis_client = None
    app.dependency_overrides[verify_service_key] = lambda: None
    app.dependency_overrides[extract_user_context] = lambda: UserContext(user_id="7", workspace_id=str(WS))
    return TestClient(app)


# ---------------------------------------------------------------------------
# Azimuth 0 is an azimuth
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("asked", "used"),
    [(0.0, 0.0), (None, 90.0), (45.0, 45.0), (-30.0, -30.0), (90.0, 90.0), (359.0, 359.0)],
)
async def test_the_reference_azimuth_is_only_defaulted_when_absent(
    monkeypatch: pytest.MonkeyPatch, asked: float | None, used: float
) -> None:
    result = await _fetch(monkeypatch, shown=3, total=3, azimuth=asked)

    assert result["reference_azimuth_deg"] == used


def test_a_north_south_section_is_drawn_and_titled_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(viz, "scoped_connection", _Scope(3, 3))

    resp = _client().post(
        "/v1/viz/chart", json={"chart_kind": "long_section", "project_id": str(PROJECT), "reference_azimuth_deg": 0}
    )

    assert resp.status_code == 200, resp.text
    layout = resp.json()["layout"]
    assert "az=0°" in layout["title"]["text"] and "az=90°" not in layout["title"]["text"]
    assert "+az=0°" in layout["xaxis"]["title"]["text"]


def test_no_azimuth_given_is_still_the_documented_east_west_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(viz, "scoped_connection", _Scope(3, 3))

    resp = _client().post("/v1/viz/chart", json={"chart_kind": "long_section", "project_id": str(PROJECT)})

    assert "az=90°" in resp.json()["layout"]["title"]["text"]


# ---------------------------------------------------------------------------
# A cut list says so
# ---------------------------------------------------------------------------
async def test_the_fetch_reports_how_many_holes_qualified(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _fetch(monkeypatch, shown=viz.LONG_SECTION_MAX_HOLES, total=312, azimuth=None)

    assert len(result["collars"]) == viz.LONG_SECTION_MAX_HOLES
    assert result["holes_total"] == 312
    assert all("holes_total" not in c for c in result["collars"]), "the count is not a collar attribute"


async def test_an_empty_project_reports_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    result = await _fetch(monkeypatch, shown=0, total=0, azimuth=None)

    assert result["collars"] == [] and result["holes_total"] == 0


def test_a_truncated_section_says_so_in_its_title_and_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(viz, "scoped_connection", _Scope(viz.LONG_SECTION_MAX_HOLES, 312))

    resp = _client().post("/v1/viz/chart", json={"chart_kind": "long_section", "project_id": str(PROJECT)})

    layout = resp.json()["layout"]
    assert "first 100 of 312 holes" in layout["title"]["text"]
    assert layout["meta"] == {"holes_shown": 100, "holes_total": 312}
    assert len(resp.json()["data"]) == 100


def test_a_complete_section_carries_no_truncation_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(viz, "scoped_connection", _Scope(40, 40))

    layout = (
        _client()
        .post("/v1/viz/chart", json={"chart_kind": "long_section", "project_id": str(PROJECT)})
        .json()["layout"]
    )

    assert "first" not in layout["title"]["text"] and "meta" not in layout


def test_the_figure_note_survives_a_custom_title() -> None:
    fig = long_section_figure(
        collars=[_row(i, 5) for i in range(2)], reference_azimuth_deg=0.0, title="Zone A", holes_total=5
    )

    assert fig["layout"]["title"]["text"] == "Zone A — first 2 of 5 holes (by hole id)"


def test_the_query_counts_before_it_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The window count is evaluated over the whole filtered set, ahead of LIMIT."""
    scope = _Scope(3, 3)
    monkeypatch.setattr(viz, "scoped_connection", scope)
    client = _client()

    client.post("/v1/viz/chart", json={"chart_kind": "long_section", "project_id": str(PROJECT)})

    (sql,) = scope.conn.sql_for("silver.collars")
    assert "count(*) OVER () AS holes_total" in sql
    assert sql.index("count(*) OVER ()") < sql.index(f"LIMIT {viz.LONG_SECTION_MAX_HOLES}")
