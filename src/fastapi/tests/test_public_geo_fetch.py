"""Fetchers, fail-loud reporting and the operator trigger for public-geo sync.

Everything network-shaped goes through ``httpx.MockTransport`` installed via
``fetch_report.make_client`` — no test here can reach a real survey (and the
sandbox these were written in could not have anyway).

Covers:
  * arcgis._get_json / iter_all_features record WHY they stopped
    (HTTP status, transport, non-JSON, in-band ArcGIS error)
  * sync_source / sync_all: per-feed `error`, row-error count + first error,
    `failed_feeds`, `all_empty`, logging at WARNING/ERROR with the feed id
  * public_geo_sync.finish_run: audit row first, then FAILED on all-empty
  * mappers: SK dispositions and SK bedrock
  * registry: SK only — no BC feed, no BC host
  * POST /internal/v1/public-geo/sync/trigger
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from app.services.public_geo import arcgis, fetch_report
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
            status_by_source_value={},
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

    async def test_sync_all_all_empty_and_failed_feeds(self, monkeypatch) -> None:
        _install(monkeypatch, lambda r: httpx.Response(503, text="down"))
        result = await S.sync_all(
            _FakeConn(), source_ids=["CA-SK-MINE-LOC", "CA-SK-SMDI", "CA-SK-GEOLOGY-BEDROCK-250K"]
        )
        assert result["feeds"] == 3
        assert result["all_empty"] is True
        assert {f["source_id"] for f in result["failed_feeds"]} == {
            "CA-SK-MINE-LOC", "CA-SK-SMDI", "CA-SK-GEOLOGY-BEDROCK-250K",
        }
        assert all("503" in f["reason"] for f in result["failed_feeds"])
        summary = S.failure_summary(result)
        assert summary.startswith("all 3 feed(s) fetched 0 features")
        assert "CA-SK-SMDI: HTTP 503" in summary

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
# Mappers
# ---------------------------------------------------------------------------


class TestMappers:
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

    def test_bedrock_without_unit_code_is_unmapped(self) -> None:
        assert S._map_bedrock_geology(
            _src("CA-SK-GEOLOGY-BEDROCK-250K"), {"id": 1, "properties": {"ERA": "x"}}, _Aliases()
        ) is None

    def test_bedrock_upsert_targets_the_existing_table(self) -> None:
        sql, cols = S.build_upsert(S.SPECS["bedrock_geology"])
        assert "public_geo.pg_bedrock_geology" in sql
        assert "scale" in cols and "ST_MakeValid" in sql


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_no_british_columbia_feed_is_synced(self) -> None:
        """Kyle, 2026-09-29: skip BC altogether. No CA-BC feed, no BC host."""
        assert [s.source_id for s in SOURCES if s.jurisdiction_code == "CA-BC"] == []
        assert {s.jurisdiction_code for s in SOURCES} == {"CA-SK"}
        assert not [s.service_url for s in SOURCES if "gov.bc.ca" in s.service_url]

    def test_ca_bc_stays_a_known_jurisdiction(self) -> None:
        """Metadata, not addressing: kept so the trigger can say "no feeds"."""
        from app.services.public_geo.registry import JURISDICTIONS

        assert JURISDICTIONS["CA-BC"].display_name == "British Columbia"

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
        assert body["feeds"] == 28
        assert dispatched[0].jurisdiction_codes is None

    def test_narrows_by_jurisdiction(self, trigger_client) -> None:
        client, dispatched = trigger_client
        r = client.post(self.URL, json={"jurisdiction_codes": ["CA-SK", "CA-SK"]}, headers=self._key())
        assert r.status_code == 202, r.text
        assert r.json()["jurisdiction_codes"] == ["CA-SK"]
        assert r.json()["feeds"] == 28
        assert dispatched[0].jurisdiction_codes == ["CA-SK"]

    def test_unknown_jurisdiction_is_422(self, trigger_client) -> None:
        client, dispatched = trigger_client
        r = client.post(self.URL, json={"jurisdiction_codes": ["XX-ZZ"]}, headers=self._key())
        assert r.status_code == 422
        assert dispatched == []

    @pytest.mark.parametrize("code", ["CA-AB", "CA-BC"])
    def test_jurisdiction_without_feeds_is_422(self, trigger_client, code) -> None:
        client, dispatched = trigger_client
        r = client.post(self.URL, json={"jurisdiction_codes": [code]}, headers=self._key())
        assert r.status_code == 422
        assert code in json.dumps(r.json())
        assert "no public-geo feeds are registered" in json.dumps(r.json())
        assert dispatched == []
