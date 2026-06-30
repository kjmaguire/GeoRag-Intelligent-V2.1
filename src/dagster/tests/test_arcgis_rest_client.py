"""Unit tests for the ArcGIS REST FeatureServer client
(georag_dagster/clients/arcgis_rest.py).

All network calls are replaced with monkeypatched stubs — no live HTTP.

Covers:
- _strip_layer_index
- _extract_wkid_from_crs
- _safe_headers
- fetch_layer_metadata (mocked _get_with_retry)
- fetch_service_metadata (mocked — 3 layers returned)
- fetch_layer_geojson pagination (2 pages, feature_count, pages_fetched)
- ArcGisPaginationStalledError when max_pages exceeded
- 5xx retry behaviour (2 failures then success / 4 failures → raises)
- 4xx never retries
- iter_layers yields (layer_id, name) pairs

Run with:  pytest tests/test_arcgis_rest_client.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

import georag_dagster.clients.arcgis_rest as arc
from georag_dagster.clients.arcgis_rest import (
    ArcGisPaginationStalledError,
    ArcGisRestError,
    FetchResult,
    LayerMetadata,
    ServiceMetadata,
    _extract_wkid_from_crs,
    _safe_headers,
    _strip_layer_index,
    fetch_layer_metadata,
    fetch_service_metadata,
    fetch_layer_geojson,
    iter_layers,
)

FIXTURES = Path(__file__).parent / "fixtures" / "arcgis"


# ---------------------------------------------------------------------------
# _strip_layer_index
# ---------------------------------------------------------------------------

class TestStripLayerIndex:
    def test_strips_numeric_suffix(self):
        url = "https://gis.example.com/arcgis/rest/services/Geo/FeatureServer/3"
        assert _strip_layer_index(url) == "https://gis.example.com/arcgis/rest/services/Geo/FeatureServer"

    def test_no_numeric_suffix_unchanged(self):
        url = "https://gis.example.com/arcgis/rest/services/Geo/FeatureServer"
        assert _strip_layer_index(url) == url

    def test_trailing_slash_tolerated(self):
        url = "https://gis.example.com/arcgis/rest/services/Geo/FeatureServer/3/"
        assert _strip_layer_index(url) == "https://gis.example.com/arcgis/rest/services/Geo/FeatureServer"

    def test_mapserver_unchanged(self):
        url = "https://gis.example.com/arcgis/rest/services/Geo/MapServer"
        assert _strip_layer_index(url) == url

    def test_layer_zero_stripped(self):
        url = "https://gis.example.com/FeatureServer/0"
        assert _strip_layer_index(url) == "https://gis.example.com/FeatureServer"

    def test_double_digit_index_stripped(self):
        url = "https://gis.example.com/FeatureServer/42"
        assert _strip_layer_index(url) == "https://gis.example.com/FeatureServer"


# ---------------------------------------------------------------------------
# _extract_wkid_from_crs
# ---------------------------------------------------------------------------

class TestExtractWkidFromCrs:
    def test_urn_ogc_form(self):
        crs = {"name": "urn:ogc:def:crs:EPSG::2957"}
        assert _extract_wkid_from_crs(crs) == 2957

    def test_short_epsg_form(self):
        crs = {"name": "EPSG:3005"}
        assert _extract_wkid_from_crs(crs) == 3005

    def test_no_epsg_returns_none(self):
        crs = {"name": "CRS84"}
        assert _extract_wkid_from_crs(crs) is None

    def test_empty_dict_returns_none(self):
        assert _extract_wkid_from_crs({}) is None

    def test_epsg_4326(self):
        crs = {"name": "urn:ogc:def:crs:EPSG::4326"}
        assert _extract_wkid_from_crs(crs) == 4326


# ---------------------------------------------------------------------------
# _safe_headers
# ---------------------------------------------------------------------------

class TestSafeHeaders:
    def _make_headers(self, pairs: list[tuple[str, str]]) -> httpx.Headers:
        return httpx.Headers(dict(pairs))

    def test_keeps_content_type(self):
        hdrs = self._make_headers([("content-type", "application/json")])
        result = _safe_headers(hdrs)
        assert result.get("content-type") == "application/json"

    def test_keeps_etag(self):
        hdrs = self._make_headers([("etag", '"abc123"')])
        result = _safe_headers(hdrs)
        assert "etag" in result

    def test_keeps_last_modified(self):
        hdrs = self._make_headers([("last-modified", "Mon, 08 Apr 2024 00:00:00 GMT")])
        result = _safe_headers(hdrs)
        assert "last-modified" in result

    def test_strips_authorization(self):
        hdrs = self._make_headers([
            ("authorization", "Bearer secret-token"),
            ("content-type", "application/json"),
        ])
        result = _safe_headers(hdrs)
        assert "authorization" not in result

    def test_strips_set_cookie(self):
        hdrs = self._make_headers([
            ("set-cookie", "session=abc; HttpOnly"),
            ("etag", '"abc"'),
        ])
        result = _safe_headers(hdrs)
        assert "set-cookie" not in result
        assert "etag" in result

    def test_strips_cookie(self):
        hdrs = self._make_headers([
            ("cookie", "session=abc"),
            ("content-type", "application/json"),
        ])
        result = _safe_headers(hdrs)
        assert "cookie" not in result


# ---------------------------------------------------------------------------
# fetch_layer_metadata — mocked _get_with_retry
# ---------------------------------------------------------------------------

def _make_mock_response(data: dict[str, Any]) -> MagicMock:
    resp = MagicMock(spec=httpx.Response)
    resp.json.return_value = data
    resp.headers = httpx.Headers({})
    resp.status_code = 200
    return resp


class TestFetchLayerMetadata:
    def test_returns_layer_metadata_with_geometry_type(self):
        fixture = json.loads((FIXTURES / "layer_metadata.json").read_text())
        mock_resp = _make_mock_response(fixture)

        with patch.object(arc, "_get_with_retry", return_value=mock_resp):
            result = fetch_layer_metadata("https://example.com/FeatureServer/3")

        assert isinstance(result, LayerMetadata)
        assert result.geometry_type == "esriGeometryPoint"

    def test_last_edit_date_ms_populated(self):
        fixture = json.loads((FIXTURES / "layer_metadata.json").read_text())
        mock_resp = _make_mock_response(fixture)

        with patch.object(arc, "_get_with_retry", return_value=mock_resp):
            result = fetch_layer_metadata("https://example.com/FeatureServer/3")

        assert result.last_edit_date_ms == 1712534400000

    def test_source_spatial_reference_wkid_populated(self):
        fixture = json.loads((FIXTURES / "layer_metadata.json").read_text())
        mock_resp = _make_mock_response(fixture)

        with patch.object(arc, "_get_with_retry", return_value=mock_resp):
            result = fetch_layer_metadata("https://example.com/FeatureServer/3")

        assert result.source_spatial_reference_wkid == 2957

    def test_layer_id_and_name_populated(self):
        fixture = json.loads((FIXTURES / "layer_metadata.json").read_text())
        mock_resp = _make_mock_response(fixture)

        with patch.object(arc, "_get_with_retry", return_value=mock_resp):
            result = fetch_layer_metadata("https://example.com/FeatureServer/3")

        assert result.layer_id == 3
        assert result.name == "Mineral Occurrences"


# ---------------------------------------------------------------------------
# fetch_service_metadata — 3 layers
# ---------------------------------------------------------------------------

class TestFetchServiceMetadata:
    def test_returns_service_metadata_with_three_layers(self):
        fixture = json.loads((FIXTURES / "service_metadata.json").read_text())
        mock_resp = _make_mock_response(fixture)

        with patch.object(arc, "_get_with_retry", return_value=mock_resp):
            result = fetch_service_metadata("https://example.com/FeatureServer")

        assert isinstance(result, ServiceMetadata)
        assert len(result.layers) == 3

    def test_service_last_edit_date_ms(self):
        fixture = json.loads((FIXTURES / "service_metadata.json").read_text())
        mock_resp = _make_mock_response(fixture)

        with patch.object(arc, "_get_with_retry", return_value=mock_resp):
            result = fetch_service_metadata("https://example.com/FeatureServer")

        assert result.service_last_edit_date_ms == 1712534400000

    def test_layer_names_correct(self):
        fixture = json.loads((FIXTURES / "service_metadata.json").read_text())
        mock_resp = _make_mock_response(fixture)

        with patch.object(arc, "_get_with_retry", return_value=mock_resp):
            result = fetch_service_metadata("https://example.com/FeatureServer")

        names = [layer.name for layer in result.layers]
        assert "Mine Locations" in names
        assert "SMDI Mineral Occurrences" in names
        assert "Drillhole Compilation" in names


# ---------------------------------------------------------------------------
# fetch_layer_geojson — paginated fetch
# ---------------------------------------------------------------------------

def _make_feature(fid: int) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": fid,
        "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
        "properties": {"OBJECTID": fid, "NAME": f"Feature {fid}"},
    }


def _page1_response() -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "exceededTransferLimit": True,
        "crs": {"properties": {"name": "urn:ogc:def:crs:EPSG::2957"}},
        "features": [_make_feature(i) for i in range(2000)],
    }


def _page2_response() -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "exceededTransferLimit": False,
        "features": [_make_feature(i + 2000) for i in range(500)],
    }


def _layer_meta_response() -> dict[str, Any]:
    return {
        "id": 1,
        "name": "Test Layer",
        "geometryType": "esriGeometryPoint",
        "sourceSpatialReference": {"wkid": 2957},
        "editingInfo": {"lastEditDate": 1712534400000},
        "maxRecordCount": 2000,
    }


def _service_meta_response() -> dict[str, Any]:
    return {
        "name": "TestService",
        "editingInfo": {"lastEditDate": 1712534400000},
        "layers": [{"id": 1, "name": "Test Layer"}],
    }


class TestFetchLayerGeoJSON:
    def _side_effect_pages(self):
        """Returns a function that alternates between metadata calls and page calls.

        Call order for fetch_layer_geojson:
          1. fetch_layer_metadata → _get_with_retry (layer meta)
          2. fetch_service_metadata → _get_with_retry (service meta)
          3. pagination call page 1
          4. pagination call page 2
        """
        call_count = [0]
        responses = [
            _layer_meta_response(),
            _service_meta_response(),
            _page1_response(),
            _page2_response(),
        ]

        def side_effect(*args, **kwargs):
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(responses):
                return _make_mock_response(responses[idx])
            # Extra calls return empty-features page (should not happen in test)
            return _make_mock_response({"type": "FeatureCollection", "features": []})

        return side_effect

    def test_feature_count_equals_sum_of_pages(self):
        with patch.object(arc, "_get_with_retry", side_effect=self._side_effect_pages()):
            result = fetch_layer_geojson("https://example.com/FeatureServer/1")

        assert isinstance(result, FetchResult)
        assert result.feature_count == 2500

    def test_pages_fetched_equals_two(self):
        with patch.object(arc, "_get_with_retry", side_effect=self._side_effect_pages()):
            result = fetch_layer_geojson("https://example.com/FeatureServer/1")

        assert result.pages_fetched == 2

    def test_spatial_reference_wkid_from_first_page(self):
        with patch.object(arc, "_get_with_retry", side_effect=self._side_effect_pages()):
            result = fetch_layer_geojson("https://example.com/FeatureServer/1")

        assert result.spatial_reference_wkid == 2957

    def test_feature_collection_type(self):
        with patch.object(arc, "_get_with_retry", side_effect=self._side_effect_pages()):
            result = fetch_layer_geojson("https://example.com/FeatureServer/1")

        assert result.feature_collection["type"] == "FeatureCollection"
        assert len(result.feature_collection["features"]) == 2500


# ---------------------------------------------------------------------------
# Pagination stall protection
# ---------------------------------------------------------------------------

class TestPaginationStall:
    def test_raises_stalled_error_after_max_pages(self):
        """Server always returns exceededTransferLimit=True — should raise."""
        def _always_exceeded(*args, **kwargs):
            # Return layer/service meta for first two calls, then always exceed
            return _make_mock_response({
                "type": "FeatureCollection",
                "exceededTransferLimit": True,
                "crs": {"properties": {"name": "urn:ogc:def:crs:EPSG::2957"}},
                # Return exactly page_size features each time so the loop
                # never breaks on the "short page" condition
                "features": [_make_feature(i) for i in range(2000)],
                # Include fields needed for layer/service meta parsing too
                "id": 1,
                "name": "Stalling Layer",
                "geometryType": "esriGeometryPoint",
                "sourceSpatialReference": {"wkid": 2957},
                "editingInfo": {"lastEditDate": None},
                "maxRecordCount": 2000,
                "layers": [],
            })

        with patch.object(arc, "_get_with_retry", side_effect=_always_exceeded):
            with pytest.raises(ArcGisPaginationStalledError):
                fetch_layer_geojson("https://example.com/FeatureServer/1")


# ---------------------------------------------------------------------------
# 5xx retry behaviour
# ---------------------------------------------------------------------------

class TestRetryBehaviour:
    def test_two_failures_then_success_returns_response(self):
        """2 × 5xx then 200 — should succeed (retries=3 by default)."""
        call_count = [0]

        def side_effect(url, *, params=None, timeout, retries=arc.DEFAULT_RETRIES, backoff=0.0):
            call_count[0] += 1
            if call_count[0] <= 2:
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 503
                resp.url = url
                resp.request = MagicMock()
                raise httpx.HTTPStatusError(
                    "Upstream 5xx: 503",
                    request=resp.request,
                    response=resp,
                )
            return _make_mock_response({"id": 1, "name": "OK"})

        with patch.object(arc, "_get_with_retry", side_effect=side_effect):
            # Directly test _get_with_retry behaviour by calling the real
            # function with a very fast backoff
            pass

        # Test the real retry logic directly using httpx mock
        fail_count = [0]

        def _real_side_effect(url, *, params=None, timeout, retries=3, backoff=0.001):
            fail_count[0] += 1
            resp_mock = MagicMock()
            resp_mock.url = url
            resp_mock.request = MagicMock()
            if fail_count[0] <= 2:
                resp_mock.status_code = 503
                raise httpx.HTTPStatusError("503", request=resp_mock.request, response=resp_mock)
            ok = MagicMock(spec=httpx.Response)
            ok.status_code = 200
            ok.json.return_value = {"id": 1, "name": "OK"}
            ok.headers = httpx.Headers({})
            return ok

        # Use a fast backoff to keep the test quick
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

            call_n = [0]

            def get_side_effect(url, params=None):
                call_n[0] += 1
                mock_resp = MagicMock(spec=httpx.Response)
                mock_resp.url = url
                mock_resp.request = MagicMock()
                if call_n[0] <= 2:
                    mock_resp.status_code = 503
                    raise httpx.HTTPStatusError(
                        "503", request=mock_resp.request, response=mock_resp
                    )
                mock_resp.status_code = 200
                mock_resp.json.return_value = {"id": 1, "name": "OK"}
                mock_resp.headers = httpx.Headers({})
                mock_resp.raise_for_status = MagicMock()
                return mock_resp

            mock_client.get.side_effect = get_side_effect

            result = arc._get_with_retry(
                "https://example.com/test", timeout=5.0, retries=3, backoff=0.0
            )
            assert result.status_code == 200

    def test_four_failures_raises_after_retries_exhausted(self):
        """4 failures with retries=3 (3+1=4 attempts total) → ArcGisRestError."""
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

            def always_fail(url, params=None):
                mock_resp = MagicMock(spec=httpx.Response)
                mock_resp.url = url
                mock_resp.request = MagicMock()
                mock_resp.status_code = 502
                raise httpx.HTTPStatusError(
                    "502", request=mock_resp.request, response=mock_resp
                )

            mock_client.get.side_effect = always_fail

            with pytest.raises(ArcGisRestError):
                arc._get_with_retry(
                    "https://example.com/fail", timeout=5.0, retries=3, backoff=0.0
                )

    def test_4xx_does_not_retry(self):
        """4xx responses raise ArcGisRestError IMMEDIATELY, no retry.

        Validates the V1.2 fix: 4xx responses are bona-fide client bugs
        (bad URL, missing layer, blocked IP) — retrying just burns the
        exponential-backoff budget before failing the same way. The
        except handler now inspects status_code and raises directly on
        4xx instead of falling through to the retry sleep.
        """
        call_count = 0

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
            mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)

            def respond_404(url, params=None):
                nonlocal call_count
                call_count += 1
                mock_resp = MagicMock(spec=httpx.Response)
                mock_resp.url = url
                mock_resp.request = MagicMock()
                mock_resp.status_code = 404
                mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                    "404", request=mock_resp.request, response=mock_resp
                )
                return mock_resp

            mock_client.get.side_effect = respond_404

            with pytest.raises(ArcGisRestError, match="Non-retryable 404"):
                arc._get_with_retry(
                    "https://example.com/notfound", timeout=5.0, retries=3, backoff=0.0
                )

        # Critical assertion: the request was issued exactly ONCE — no
        # retry. retries=3 would give 4 total calls under the old (buggy)
        # behaviour.
        assert call_count == 1


# ---------------------------------------------------------------------------
# iter_layers
# ---------------------------------------------------------------------------

class TestIterLayers:
    def test_yields_layer_id_name_pairs(self):
        fixture = json.loads((FIXTURES / "service_metadata.json").read_text())
        mock_resp = _make_mock_response(fixture)

        with patch.object(arc, "_get_with_retry", return_value=mock_resp):
            service_meta = fetch_service_metadata("https://example.com/FeatureServer")

        pairs = list(iter_layers(service_meta))
        assert len(pairs) == 3
        assert all(isinstance(layer_id, int) for layer_id, _ in pairs)
        assert all(isinstance(name, str) for _, name in pairs)

    def test_layer_ids_match_fixture(self):
        fixture = json.loads((FIXTURES / "service_metadata.json").read_text())
        mock_resp = _make_mock_response(fixture)

        with patch.object(arc, "_get_with_retry", return_value=mock_resp):
            service_meta = fetch_service_metadata("https://example.com/FeatureServer")

        pairs = list(iter_layers(service_meta))
        ids = [layer_id for layer_id, _ in pairs]
        assert sorted(ids) == [0, 1, 2]

    def test_empty_service_yields_nothing(self):
        empty_meta = ServiceMetadata(
            name="Empty",
            service_last_edit_date_ms=None,
            layers=[],
            raw={},
        )
        assert list(iter_layers(empty_meta)) == []
