"""Fetchers, fail-loud reporting and the operator trigger for public-geo sync.

Everything network-shaped goes through ``httpx.MockTransport`` installed via
``fetch_report.make_client`` — no test here can reach a real survey (and the
sandbox these were written in could not have anyway).

Covers:
  * arcgis._get_json / iter_all_features record WHY they stopped
    (HTTP status, transport, non-JSON, in-band ArcGIS error)
  * wfs: request shape (pub: typeName, sortBy, CQL, paging), OGC exception
    bodies, axis-order guard, stable-id promotion
  * sync_source / sync_all: per-feed `error`, row-error count + first error,
    `failed_feeds`, `all_empty`, logging at WARNING/ERROR with the feed id
  * public_geo_sync.finish_run: audit row first, then FAILED on all-empty
  * BC mappers: MINFILE mines, MTA tenure, bedrock (SK + BC)
  * POST /internal/v1/public-geo/sync/trigger
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from app.services.public_geo import arcgis, fetch_report, wfs
from app.services.public_geo import sync as S
from app.services.public_geo.fetch_report import FetchReport
from app.services.public_geo.registry import SOURCES, source_by_id

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _install(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Route every public-geo HTTP call through ``handler``; return the log."""
    seen: list[httpx.Request] = []

    def _wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        fetch_report,
        "make_client",
        lambda timeout_s: httpx.AsyncClient(transport=httpx.MockTransport(_wrapped)),
    )
    return seen


def _src(source_id: str):
    src = source_by_id(source_id)
    assert src is not None, f"{source_id} missing from registry"
    return src


def _fc(features: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features, **extra}


def _pt(fid: Any, props: dict[str, Any], lon: float = -120.5, lat: float = 50.2) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": fid,
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


class _Aliases(S.AliasTables):
    def __init__(self) -> None:
        super().__init__(
            grouping_by_alias={"gold": "precious_metals", "copper": "base_metals"},
            status_by_source_value={
                ("CA-BC", "mine", "producer"): "producing",
                ("CA-BC", "mine", "past producer"): "past-producer",
            },
        )


class _FakeConn:
    """Enough of asyncpg.Connection for sync_source."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.executed: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return []  # no varchar limits, no alias rows

    async def execute(self, sql: str, *args: Any) -> str:
        # source_feature_id is the 3rd common column; find it by value shape.
        if any(str(a) in self.fail_on for a in args):
            raise ValueError("value too long for type character varying(16)")
        self.executed.append(args)
        return "INSERT 0 1"


# ---------------------------------------------------------------------------
# ArcGIS: failures carry a reason
# ---------------------------------------------------------------------------


class TestArcgisReasons:
    async def test_http_status_is_recorded(self, monkeypatch) -> None:
        _install(monkeypatch, lambda r: httpx.Response(403, text="Forbidden"))
        report = FetchReport()
        out = await arcgis._get_json(
            "https://x.example/MapServer/1/query", {}, timeout_s=1, source_id="T", report=report
        )
        assert out is None
        assert report.error_kind == "http_status"
        assert report.http_status == 403
        assert "403" in (report.error or "")

    async def test_transport_error_is_recorded_with_its_type(self, monkeypatch) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("proxy refused", request=request)

        _install(monkeypatch, boom)
        report = FetchReport()
        assert await arcgis._get_json("https://x.example/q", {}, timeout_s=1, source_id="T", report=report) is None
        assert report.error_kind == "transport"
        assert report.error is not None and report.error.startswith("ConnectError")

    async def test_in_band_arcgis_error_is_recorded(self, monkeypatch) -> None:
        body = {"error": {"code": 400, "message": "Invalid query parameters", "details": ["bad where"]}}
        _install(monkeypatch, lambda r: httpx.Response(200, json=body))
        report = FetchReport()
        assert await arcgis._get_json("https://x.example/q", {}, timeout_s=1, source_id="T", report=report) is None
        assert report.error_kind == "arcgis_error"
        assert "Invalid query parameters" in (report.error or "")
        assert "bad where" in (report.error or "")

    async def test_non_json_body_is_recorded(self, monkeypatch) -> None:
        _install(
            monkeypatch,
            lambda r: httpx.Response(200, text="<html>gateway</html>", headers={"content-type": "text/html"}),
        )
        report = FetchReport()
        assert await arcgis._get_json("https://x.example/q", {}, timeout_s=1, source_id="T", report=report) is None
        assert report.error_kind == "invalid_json"

    async def test_first_failure_wins(self) -> None:
        r = FetchReport()
        r.fail("http_status", "HTTP 500", http_status=500)
        r.fail("transport", "later")
        assert r.error == "HTTP 500" and r.error_kind == "http_status"
        assert r.as_stats() == {"error": "HTTP 500", "error_kind": "http_status", "http_status": 500}
        assert FetchReport().as_stats() == {}

    async def test_walk_counts_pages_and_records_a_mid_walk_failure(self, monkeypatch) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    200, json=_fc([_pt(1, {}), _pt(2, {})], properties={"exceededTransferLimit": True})
                )
            return httpx.Response(502, text="Bad Gateway")

        _install(monkeypatch, handler)
        report = FetchReport()
        got = [f async for f in arcgis.iter_all_features(_src("CA-SK-MINE-LOC"), page_size=2, report=report)]
        assert len(got) == 2
        assert report.pages == 1
        assert report.error_kind == "http_status" and report.http_status == 502


# ---------------------------------------------------------------------------
# WFS
# ---------------------------------------------------------------------------


class TestWfs:
    def test_get_feature_params(self) -> None:
        src = _src("CA-BC-MINFILE-MINES")
        p = wfs.get_feature_params(src, count=500, start_index=1000)
        assert p["SERVICE"] == "WFS" and p["VERSION"] == "2.0.0" and p["REQUEST"] == "GetFeature"
        assert p["typeNames"] == "pub:WHSE_MINERAL_TENURE.MINFIL_MINERAL_OCCURRENCE"
        assert p["outputFormat"] == "application/json"
        assert p["srsName"] == "EPSG:4326"
        assert p["count"] == 500 and p["startIndex"] == 1000
        assert p["sortBy"] == "MINFILE_NUMBER"
        assert "Producer" in p["CQL_FILTER"]

    def test_existing_namespace_is_not_doubled(self) -> None:
        from dataclasses import replace

        src = replace(_src("CA-BC-MTA-TENURE"), bcgw_object_name="pub:WHSE_X.Y")
        assert wfs.type_name(src) == "pub:WHSE_X.Y"

    async def test_pages_until_number_matched_and_promotes_stable_ids(self, monkeypatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            start = int(request.url.params["startIndex"])
            feats = [
                _pt(f"WHSE.fid-{start + i}", {"MINFILE_NUMBER": f"082F {start + i:03d}"})
                for i in range(2 if start == 0 else 1)
            ]
            return httpx.Response(200, json=_fc(feats, numberMatched=3, numberReturned=len(feats)))

        seen = _install(monkeypatch, handler)
        report = FetchReport()
        got = [
            f async for f in wfs.iter_all_features(_src("CA-BC-MINFILE-MINES"), page_size=2, report=report)
        ]
        assert [f["id"] for f in got] == ["082F 000", "082F 001", "082F 002"]
        assert report.error is None and report.pages == 2
        assert [r.url.params["startIndex"] for r in seen] == ["0", "2"]
        assert all(str(r.url).startswith("https://openmaps.gov.bc.ca/geo/ows") for r in seen)

    async def test_ogc_exception_report_under_http_200_is_a_failure(self, monkeypatch) -> None:
        xml = (
            '<?xml version="1.0"?><ows:ExceptionReport><ows:Exception exceptionCode="InvalidParameterValue">'
            "<ows:ExceptionText>Illegal property name: STATUS_DESCRIPTION</ows:ExceptionText>"
            "</ows:Exception></ows:ExceptionReport>"
        )
        _install(monkeypatch, lambda r: httpx.Response(200, text=xml, headers={"content-type": "application/xml"}))
        report = FetchReport()
        got = [f async for f in wfs.iter_all_features(_src("CA-BC-MINFILE-MINES"), report=report)]
        assert got == []
        assert report.error_kind == "wfs_exception"
        assert "InvalidParameterValue" in (report.error or "")
        assert "STATUS_DESCRIPTION" in (report.error or "")

    async def test_http_403_is_a_failure(self, monkeypatch) -> None:
        _install(monkeypatch, lambda r: httpx.Response(403, text="denied", headers={"content-type": "text/plain"}))
        report = FetchReport()
        assert [f async for f in wfs.iter_all_features(_src("CA-BC-MTA-TENURE"), report=report)] == []
        assert report.error_kind == "http_status" and report.http_status == 403

    async def test_swapped_axes_fail_the_feed(self, monkeypatch) -> None:
        feat = _pt("x", {"MINFILE_NUMBER": "1"}, lon=50.2, lat=-120.5)  # (lat, lon)
        _install(monkeypatch, lambda r: httpx.Response(200, json=_fc([feat], numberMatched=1)))
        report = FetchReport()
        assert [f async for f in wfs.iter_all_features(_src("CA-BC-MINFILE-MINES"), report=report)] == []
        assert report.error_kind == "axis_order"

    def test_axis_guard_leaves_genuine_lon_lat_alone(self) -> None:
        assert not wfs.looks_axis_swapped((-120.5, 50.2))
        assert wfs.looks_axis_swapped((50.2, -120.5))
        assert not wfs.looks_axis_swapped(None)
        poly = {"type": "MultiPolygon", "coordinates": [[[[-121.0, 49.0], [-120.0, 49.0], [-121.0, 49.0]]]]}
        assert wfs._first_xy(poly) == (-121.0, 49.0)


# ---------------------------------------------------------------------------
# sync_source / sync_all
# ---------------------------------------------------------------------------


class TestFailLoudSync:
    async def test_fetch_error_lands_in_stats_and_logs_error(self, monkeypatch, caplog) -> None:
        _install(monkeypatch, lambda r: httpx.Response(403, text="nope"))
        caplog.set_level(logging.WARNING)
        stats = await S.sync_source(_FakeConn(), _src("CA-SK-MINE-LOC"), aliases=_Aliases())
        assert stats["fetched"] == 0
        assert stats["error_kind"] == "http_status"
        assert "403" in stats["error"]
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("CA-SK-MINE-LOC" in r.getMessage() and "403" in r.getMessage() for r in errors)

    async def test_empty_feed_is_flagged_not_silent(self, monkeypatch, caplog) -> None:
        _install(monkeypatch, lambda r: httpx.Response(200, json=_fc([])))
        caplog.set_level(logging.WARNING)
        stats = await S.sync_source(_FakeConn(), _src("CA-SK-MINE-LOC"), aliases=_Aliases())
        assert stats["error_kind"] == "empty"
        assert any(
            r.levelno == logging.WARNING and "CA-SK-MINE-LOC" in r.getMessage() and "0 features" in r.getMessage()
            for r in caplog.records
        )

    async def test_row_errors_carry_count_and_first_error(self, monkeypatch) -> None:
        feats = [_pt(i, {"NAME": f"Mine {i}", "STATUS": "x"}) for i in (1, 2, 3, 4, 5)]
        _install(monkeypatch, lambda r: httpx.Response(200, json=_fc(feats, properties={"exceededTransferLimit": False})))
        conn = _FakeConn(fail_on={"2", "4"})
        stats = await S.sync_source(conn, _src("CA-SK-MINE-LOC"), aliases=_Aliases())
        assert stats["fetched"] == 5
        assert stats["upserted"] == 3
        assert stats["errors"] == 2
        assert stats["first_error"].startswith("feature 2: ValueError")
        assert "error" not in stats  # the FETCH was fine

    async def test_wfs_feed_dispatches_to_wfs(self, monkeypatch) -> None:
        feats = [
            _pt("fid-1", {"MINFILE_NUMBER": "092HSE001", "MINFILE_NAME1": "Copper Mountain",
                          "STATUS_DESCRIPTION": "Producer", "COMMODITY_DESCRIPTION1": "Copper",
                          "COMMODITY_DESCRIPTION2": "Gold"}),
        ]
        seen = _install(monkeypatch, lambda r: httpx.Response(200, json=_fc(feats, numberMatched=1)))
        conn = _FakeConn()
        stats = await S.sync_source(conn, _src("CA-BC-MINFILE-MINES"), aliases=_Aliases())
        assert stats["upserted"] == 1 and "error" not in stats
        assert seen[0].url.params["typeNames"].startswith("pub:WHSE_MINERAL_TENURE")
        # source_feature_id is the MINFILE number, not GeoServer's fid.
        assert "092HSE001" in conn.executed[0]

    async def test_sync_all_all_empty_and_failed_feeds(self, monkeypatch) -> None:
        _install(monkeypatch, lambda r: httpx.Response(503, text="down"))
        result = await S.sync_all(_FakeConn(), jurisdiction_codes=["CA-BC"])
        assert result["feeds"] >= 4
        assert result["all_empty"] is True
        assert {f["source_id"] for f in result["failed_feeds"]} >= {
            "CA-BC-MINFILE", "CA-BC-MINFILE-MINES", "CA-BC-MTA-TENURE", "CA-BC-GEOLOGY-BEDROCK",
        }
        assert all("503" in f["reason"] for f in result["failed_feeds"])
        summary = S.failure_summary(result)
        assert summary.startswith(f"all {result['feeds']} feed(s) fetched 0 features")
        assert "CA-BC-MTA-TENURE: HTTP 503" in summary

    async def test_sync_all_includes_bedrock_by_default(self, monkeypatch) -> None:
        _install(monkeypatch, lambda r: httpx.Response(200, json=_fc([])))
        result = await S.sync_all(_FakeConn(), jurisdiction_codes=["CA-SK"])
        ids = {s["source_id"] for s in result["per_source"]}
        assert "CA-SK-GEOLOGY-BEDROCK-250K" in ids
        assert result["skipped"] == []

    async def test_failure_summary_for_no_matching_feeds(self) -> None:
        assert S.failure_summary({"feeds": 0}) == "no registry feed matched the requested filters"


# ---------------------------------------------------------------------------
# Workflow: audit first, then FAILED
# ---------------------------------------------------------------------------


class TestWorkflowFinish:
    @pytest.fixture
    def audits(self, monkeypatch) -> list[dict[str, Any]]:
        from app.hatchet_workflows import public_geo_sync as W

        rows: list[dict[str, Any]] = []

        async def _emit(pool: Any, **kw: Any) -> None:
            rows.append(kw)

        monkeypatch.setattr(W, "emit_audit", _emit)
        return rows

    @staticmethod
    def _result(**over: Any) -> dict[str, Any]:
        base = {
            "feeds": 2, "fetched": 0, "upserted": 0, "errors": 0, "first_error": None,
            "skipped": [], "per_source": [],
            "failed_feeds": [
                {"source_id": "A", "fetched": 0, "reason": "HTTP 403 Forbidden"},
                {"source_id": "B", "fetched": 0, "reason": "ConnectError: refused"},
            ],
            "all_empty": True,
        }
        base.update(over)
        return base

    async def test_all_empty_writes_audit_then_raises(self, audits) -> None:
        from app.hatchet_workflows import public_geo_sync as W

        with pytest.raises(W.PublicGeoSyncFailed, match="A: HTTP 403 Forbidden"):
            await W.finish_run(None, self._result(), t0=0.0, input=W.PublicGeoSyncInput())
        assert len(audits) == 1
        assert audits[0]["action_type"] == "public_geo.sync.failed"
        assert audits[0]["payload"]["failed_feeds"][1]["reason"] == "ConnectError: refused"

    async def test_no_matching_feeds_is_non_retryable(self, audits) -> None:
        from hatchet_sdk import NonRetryableException

        from app.hatchet_workflows import public_geo_sync as W

        with pytest.raises(NonRetryableException):
            await W.finish_run(
                None, self._result(feeds=0, failed_feeds=[]), t0=0.0,
                input=W.PublicGeoSyncInput(jurisdiction_codes=["CA-AB"]),
            )
        assert audits[0]["payload"]["filters"] == {"jurisdiction_codes": ["CA-AB"]}

    async def test_partial_success_completes_and_reports(self, audits) -> None:
        from app.hatchet_workflows import public_geo_sync as W

        out = await W.finish_run(
            None,
            self._result(fetched=10, upserted=9, errors=1, first_error="B: feature 7: boom", all_empty=False,
                         failed_feeds=[{"source_id": "A", "fetched": 0, "reason": "HTTP 403"}]),
            t0=0.0, input=W.PublicGeoSyncInput(),
        )
        assert out.fetched == 10 and out.errors == 1 and out.first_error == "B: feature 7: boom"
        assert out.failed_feeds[0]["source_id"] == "A"
        assert audits[0]["action_type"] == "public_geo.sync.complete"

    def test_workflow_is_single_flight(self) -> None:
        from pathlib import Path

        from app.hatchet_workflows import public_geo_sync as W

        src = Path(W.__file__).read_text(encoding="utf-8")
        assert "ConcurrencyLimitStrategy.CANCEL_NEWEST" in src
        assert "max_runs=1" in src


# ---------------------------------------------------------------------------
# BC mappers
# ---------------------------------------------------------------------------


class TestBcMappers:
    def test_minfile_mine(self) -> None:
        row = S._map_mine(
            _src("CA-BC-MINFILE-MINES"),
            {"id": "092HSE001", "properties": {
                "MINFILE_NUMBER": "092HSE001", "MINFILE_NAME1": "Copper Mountain",
                "STATUS_DESCRIPTION": "Producer",
                "COMMODITY_DESCRIPTION1": "Copper   ", "COMMODITY_DESCRIPTION2": "Gold",
            }},
            _Aliases(),
        )
        assert row is not None
        assert row["name"] == "Copper Mountain"
        assert row["_status_raw"] == "Producer"
        assert row["commodities"] == ["Copper", "Gold"]
        assert row["commodity_grouping"] == "base_metals"
        assert row["operator"] is None
        assert row["source_feature_id"] == "092HSE001"

    @pytest.mark.parametrize(
        ("props", "expected"),
        [
            ({"TENURE_TYPE_DESCRIPTION": "Mineral", "TITLE_TYPE_DESCRIPTION": "Claim"}, ("mineral", "active")),
            ({"TENURE_TYPE_DESCRIPTION": "Coal", "TITLE_TYPE_DESCRIPTION": "Licence Application"}, ("coal", "pending")),
            ({"TENURE_TYPE_CODE": "M", "TENURE_SUB_TYPE_DESCRIPTION": "Mining Lease"}, ("mineral", "active")),
        ],
    )
    def test_mta_tenure_type_and_status(self, props, expected) -> None:
        full = {"TENURE_NUMBER_ID": 1234567, "AREA_IN_HECTARES": 20.5,
                "ISSUE_DATE": "2021-03-04Z", "GOOD_TO_DATE": "2027-03-04Z", **props}
        row = S._map_mineral_disposition(_src("CA-BC-MTA-TENURE"), {"id": "1234567", "properties": full}, _Aliases())
        assert row is not None
        assert (row["disposition_type"], row["status"]) == expected
        assert row["disposition_number"] == "1234567"
        assert str(row["area_ha"]) == "20.50"
        assert row["issue_date"].isoformat() == "2021-03-04"
        assert row["expiry_date"].isoformat() == "2027-03-04"

    def test_placer_tenure_is_unmapped_not_mislabelled(self) -> None:
        row = S._map_mineral_disposition(
            _src("CA-BC-MTA-TENURE"),
            {"id": "9", "properties": {"TENURE_NUMBER_ID": 9, "TENURE_TYPE_DESCRIPTION": "Placer"}},
            _Aliases(),
        )
        assert row is None

    def test_sk_dispositions_unchanged(self) -> None:
        row = S._map_mineral_disposition(
            _src("CA-SK-MINERAL-DISPOSITION-MINING-4"), {"id": 1, "properties": {"DISPOSITIO": "X"}}, _Aliases()
        )
        assert row is not None and (row["disposition_type"], row["status"]) == ("mineral", "lapsed")

    def test_bedrock_sk(self) -> None:
        row = S._map_bedrock_geology(
            _src("CA-SK-GEOLOGY-BEDROCK-250K"),
            {"id": 5, "properties": {"ROCK_CODE": "Ag", "NAME": "Granite", "ERA": "Paleoproterozoic",
                                     "GROUP_": "Wollaston", "DOMAIN": "Mudjatik", "LITHOLOGY": "granite"}},
            _Aliases(),
        )
        assert row is not None
        assert row["unit_code"] == "Ag" and row["unit_name"] == "Granite"
        assert row["group_name"] == "Wollaston" and row["structural_domain"] == "Mudjatik"
        assert row["scale"] == "250K"

    def test_bedrock_bc(self) -> None:
        row = S._map_bedrock_geology(
            _src("CA-BC-GEOLOGY-BEDROCK"),
            {"id": 77, "properties": {"STRAT_UNIT": "uTrNi", "STRAT_AGE": "Upper Triassic",
                                      "GP_SUITE": "Nicola Group", "TERRANE": "Quesnel",
                                      "ROCK_TYPE": "andesitic volcanic rocks"}},
            _Aliases(),
        )
        assert row is not None
        assert row["unit_code"] == "uTrNi"
        assert row["period"] == "Upper Triassic"
        assert row["group_name"] == "Nicola Group"
        assert row["structural_domain"] == "Quesnel"
        assert row["lithology"] == "andesitic volcanic rocks"
        assert row["scale"] == "50-250K"

    def test_bedrock_without_unit_code_is_unmapped(self) -> None:
        assert S._map_bedrock_geology(
            _src("CA-BC-GEOLOGY-BEDROCK"), {"id": 1, "properties": {"ERA": "x"}}, _Aliases()
        ) is None

    def test_bedrock_upsert_targets_the_existing_table(self) -> None:
        sql, cols = S.build_upsert(S.SPECS["bedrock_geology"])
        assert "public_geo.pg_bedrock_geology" in sql
        assert "scale" in cols and "ST_MakeValid" in sql


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_every_wfs_feed_is_fully_addressed(self) -> None:
        wfs_feeds = [s for s in SOURCES if s.protocol == "wfs"]
        assert {s.source_id for s in wfs_feeds} == {
            "CA-BC-MINFILE-MINES", "CA-BC-MTA-TENURE", "CA-BC-GEOLOGY-BEDROCK",
        }
        for s in wfs_feeds:
            assert s.is_queryable
            assert s.bcgw_object_name and s.bcgw_object_name.startswith("WHSE_")
            assert s.id_field, f"{s.source_id}: WFS paging needs a stable sortBy key"
            assert s.catalogue_slug
            assert s.canonical_type in S.SPECS
            assert s.service_url == "https://openmaps.gov.bc.ca/geo/ows"
            assert s.verified is False  # until a probe run in AWS says otherwise

    def test_minfile_keeps_its_source_id_and_arcgis_path(self) -> None:
        s = _src("CA-BC-MINFILE")
        assert s.protocol == "arcgis"
        assert s.service_url.endswith("/MapServer/137")
        assert s.bcgw_object_name == "WHSE_MINERAL_TENURE.MINFIL_MINERAL_OCCURRENCE"

    def test_every_source_type_has_a_mapper(self) -> None:
        for s in SOURCES:
            if s.is_queryable:
                assert s.canonical_type in S.MAPPERS, s.source_id


# ---------------------------------------------------------------------------
# Operator trigger endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def trigger_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.routers import public_geo_trigger as T

    dispatched: list[Any] = []

    class _Ref:
        workflow_run_id = "run-123"

    async def _run_no_wait(inp: Any) -> _Ref:
        dispatched.append(inp)
        return _Ref()

    monkeypatch.setattr(T.public_geo_sync, "aio_run_no_wait", _run_no_wait)
    app = FastAPI()
    app.include_router(T.router)
    return TestClient(app), dispatched


class TestTriggerEndpoint:
    URL = "/internal/v1/public-geo/sync/trigger"

    def _key(self) -> dict[str, str]:
        from app.config import settings

        return {"X-Service-Key": settings.FASTAPI_SERVICE_KEY}

    def test_requires_service_key(self, trigger_client) -> None:
        client, dispatched = trigger_client
        assert client.post(self.URL, json={}, headers={"X-Service-Key": "wrong"}).status_code == 401
        assert dispatched == []

    def test_dispatches_all_and_returns_run_id(self, trigger_client) -> None:
        client, dispatched = trigger_client
        r = client.post(self.URL, json={}, headers=self._key())
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["workflow_run_id"] == "run-123"
        assert body["jurisdiction_codes"] is None
        assert body["feeds"] > 30
        assert dispatched[0].jurisdiction_codes is None

    def test_narrows_by_jurisdiction(self, trigger_client) -> None:
        client, dispatched = trigger_client
        r = client.post(self.URL, json={"jurisdiction_codes": ["CA-BC", "CA-BC"]}, headers=self._key())
        assert r.status_code == 202, r.text
        assert r.json()["jurisdiction_codes"] == ["CA-BC"]
        assert r.json()["feeds"] == 4
        assert dispatched[0].jurisdiction_codes == ["CA-BC"]

    def test_unknown_jurisdiction_is_422(self, trigger_client) -> None:
        client, dispatched = trigger_client
        r = client.post(self.URL, json={"jurisdiction_codes": ["XX-ZZ"]}, headers=self._key())
        assert r.status_code == 422
        assert dispatched == []

    def test_jurisdiction_without_feeds_is_422(self, trigger_client) -> None:
        client, dispatched = trigger_client
        r = client.post(self.URL, json={"jurisdiction_codes": ["CA-AB"]}, headers=self._key())
        assert r.status_code == 422
        assert "CA-AB" in json.dumps(r.json())
        assert dispatched == []
