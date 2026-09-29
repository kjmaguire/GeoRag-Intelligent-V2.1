"""Async WFS 2.0 GeoJSON client for the BC Geographic Warehouse (DataBC).

Why BC feeds use WFS rather than the ArcGIS path
------------------------------------------------
Every other public-geo feed is an ArcGIS REST ``MapServer/<n>/query``. The
BC feeds are not, deliberately. A MapServer layer is addressed by a NUMBER
(``bcgwpub/MapServer/137``) that the publisher can renumber whenever it
republishes the map document, and nothing in the number says what it is — the
registry has carried ``137`` as "MINFILE" without anyone being able to confirm
it from here. DataBC's public WFS is addressed by the BC Geographic Warehouse
OBJECT NAME itself (``WHSE_MINERAL_TENURE.MTA_ACQUIRED_TENURE_SVW``), which is
the stable identifier the BC Data Catalogue records for each dataset
(``object_name`` in CKAN's ``package_show``). So the address IS the identity:
there is no layer-number resolution step to cache, drift, or guess.

Request shape (GeoServer, ``https://openmaps.gov.bc.ca/geo/ows``)::

    SERVICE=WFS  VERSION=2.0.0  REQUEST=GetFeature
    typeNames=pub:<BCGW object>          GeoServer namespaces BCGW as `pub:`
    outputFormat=application/json        GeoJSON FeatureCollection
    srsName=EPSG:4326                    reprojected server-side, lon/lat
    sortBy=<stable key>                  paging is only deterministic when sorted
    count=<page>  startIndex=<offset>    WFS 2.0 paging
    CQL_FILTER=<expr>                    optional per-feed attribute filter

Failure handling matches ``arcgis``: nothing raises into the sync, but every
early stop records WHY in the caller's ``FetchReport`` — HTTP status,
transport error, non-JSON body, or an OGC ``ExceptionReport`` (GeoServer
answers a bad typeName / sortBy / CQL with XML, sometimes under HTTP 200).

A coordinate-order guard runs on the first page: WFS 2.0 with some srsName
spellings returns lat/lon, which would store every BC feature in the Arctic
Ocean off Siberia while reporting a clean sync. A first coordinate that looks
swapped fails the feed loudly instead.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.services.public_geo import fetch_report
from app.services.public_geo.fetch_report import FetchReport, describe_exception
from app.services.public_geo.registry import PublicGeoSource

logger = logging.getLogger(__name__)

#: GeoServer's namespace for the public BCGW layers.
BCGW_NAMESPACE = "pub"

_EXCEPTION_TEXT = re.compile(r"<(?:ows:)?ExceptionText>(.*?)</(?:ows:)?ExceptionText>", re.S)
_EXCEPTION_CODE = re.compile(r'exceptionCode="([^"]+)"')


def type_name(source: PublicGeoSource) -> str:
    """``pub:WHSE_X.Y`` — adds the namespace unless the registry already has one."""
    obj = source.bcgw_object_name or ""
    return obj if ":" in obj else f"{BCGW_NAMESPACE}:{obj}"


def get_feature_params(
    source: PublicGeoSource,
    *,
    count: int,
    start_index: int = 0,
) -> dict[str, Any]:
    """GetFeature query parameters for one page of ``source``."""
    params: dict[str, Any] = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "typeNames": type_name(source),
        "outputFormat": "application/json",
        "srsName": "EPSG:4326",
        "count": int(count),
        "startIndex": int(start_index),
    }
    if source.id_field:
        params["sortBy"] = source.id_field
    if source.cql_filter:
        params["CQL_FILTER"] = source.cql_filter
    return params


def _ogc_exception(text: str) -> str | None:
    """The message from an OGC ExceptionReport body, or None if it is not one."""
    if "ExceptionReport" not in text and "ServiceException" not in text:
        return None
    code = _EXCEPTION_CODE.search(text)
    msgs = [m.strip() for m in _EXCEPTION_TEXT.findall(text) if m.strip()]
    msg = " | ".join(msgs) or text.strip()[:200]
    return f"WFS exception {code.group(1) if code else ''}: {msg}".replace("  ", " ")[:400]


async def _get_page(
    client: httpx.AsyncClient,
    source: PublicGeoSource,
    params: dict[str, Any],
    *,
    timeout_s: float,
    report: FetchReport,
) -> dict[str, Any] | None:
    """One GetFeature page, or None with the reason recorded."""
    sid = source.source_id
    try:
        resp = await client.get(source.service_url, params=params, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 — degrade, never propagate; reason is recorded
        report.fail("transport", describe_exception(exc))
        logger.warning("public_geo.wfs: %s request failed: %s", sid, report.error)
        return None

    body_text = resp.text
    ogc = _ogc_exception(body_text[:20000]) if "json" not in resp.headers.get(
        "content-type", ""
    ) else None
    if ogc is not None:
        report.fail("wfs_exception", ogc, http_status=resp.status_code)
        logger.warning("public_geo.wfs: %s: %s", sid, report.error)
        return None

    if resp.status_code >= 400:
        report.fail(
            "http_status",
            f"HTTP {resp.status_code} {resp.reason_phrase}".strip(),
            http_status=resp.status_code,
        )
        logger.warning("public_geo.wfs: %s request failed: %s", sid, report.error)
        return None

    try:
        payload = resp.json()
    except ValueError:
        report.fail(
            "invalid_json",
            f"non-JSON body ({resp.headers.get('content-type', '?')}): {body_text[:160]!r}",
            http_status=resp.status_code,
        )
        logger.warning("public_geo.wfs: %s returned a non-JSON body: %s", sid, report.error)
        return None
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        report.fail(
            "invalid_json",
            f"expected a GeoJSON FeatureCollection, got {str(payload)[:160]!r}",
            http_status=resp.status_code,
        )
        logger.warning("public_geo.wfs: %s: %s", sid, report.error)
        return None
    return payload


def _first_xy(geometry: Any) -> tuple[float, float] | None:
    """First coordinate pair of any GeoJSON geometry, for the axis-order guard."""
    if not isinstance(geometry, dict):
        return None
    coords: Any = geometry.get("coordinates")
    if coords is None and geometry.get("geometries"):
        return _first_xy(geometry["geometries"][0])
    while isinstance(coords, list) and coords and isinstance(coords[0], list):
        coords = coords[0]
    if isinstance(coords, list) and len(coords) >= 2:
        try:
            return float(coords[0]), float(coords[1])
        except (TypeError, ValueError):
            # Unparseable coordinates just skip the axis-order check for this
            # feature; the row itself is validated again by the sync mapper.
            logger.debug("public_geo.wfs: non-numeric coordinates %r", coords[:2], exc_info=True)
            return None
    return None


def looks_axis_swapped(xy: tuple[float, float] | None) -> bool:
    """True when a BC coordinate came back as (lat, lon).

    British Columbia spans roughly lon -139..-114, lat 48..60. A pair whose
    first value is a plausible BC latitude and second a plausible BC
    longitude is swapped; anything else (including a genuine lon/lat) is not.
    """
    if xy is None:
        return False
    x, y = xy
    return 40.0 <= x <= 70.0 and -145.0 <= y <= -105.0


def _stable_id(feature: dict[str, Any], id_field: str | None) -> str | None:
    """The registry's declared key, falling back to OBJECTID then GeoServer's fid.

    GeoServer's own feature id (``WHSE_X.fid-3a9c...``) is not stable for
    views without a primary key, which is why every WFS feed declares an
    ``id_field`` and the value is promoted into ``feature["id"]`` — the slot
    ``arcgis.object_id_of`` and the sync's natural key read.
    """
    props = feature.get("properties") or {}
    lowered = {str(k).lower(): v for k, v in props.items()}
    for key in (id_field, "OBJECTID"):
        if not key:
            continue
        val = lowered.get(key.lower())
        if val not in (None, ""):
            return str(val).strip()
    fid = feature.get("id")
    return str(fid) if fid not in (None, "") else None


async def iter_all_features(
    source: PublicGeoSource,
    *,
    page_size: int = 1000,
    max_features: int | None = None,
    timeout_s: float = 120.0,
    report: FetchReport | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield every feature of a WFS type, paging with ``startIndex``/``count``.

    Same contract as ``arcgis.iter_all_features``: GeoJSON features with a
    stable ``id``, ending quietly on failure with the reason in ``report``.
    """
    sink = report if report is not None else FetchReport()
    if not source.bcgw_object_name:
        sink.fail("config", f"{source.source_id} has protocol=wfs but no bcgw_object_name")
        logger.error("public_geo.wfs: %s", sink.error)
        return

    offset = 0
    yielded = 0
    seen: set[str] = set()
    checked_axes = False

    async with fetch_report.make_client(timeout_s) as client:
        while True:
            params = get_feature_params(source, count=page_size, start_index=offset)
            payload = await _get_page(client, source, params, timeout_s=timeout_s, report=sink)
            if payload is None:
                return
            sink.pages += 1

            features = [f for f in (payload.get("features") or []) if isinstance(f, dict)]
            if not features:
                return

            if not checked_axes:
                checked_axes = True
                xy = _first_xy(features[0].get("geometry"))
                if looks_axis_swapped(xy):
                    sink.fail(
                        "axis_order",
                        f"first coordinate {xy} looks like (lat, lon) — refusing to "
                        "store BC features with swapped axes",
                    )
                    logger.error("public_geo.wfs: %s: %s", source.source_id, sink.error)
                    return

            fresh = 0
            for f in features:
                fid = _stable_id(f, source.id_field)
                key = fid or f"{offset}:{fresh}"
                if key in seen:
                    continue
                seen.add(key)
                if fid is not None:
                    f["id"] = fid
                fresh += 1
                yielded += 1
                yield f
                if max_features is not None and yielded >= max_features:
                    return

            if fresh == 0:
                logger.warning(
                    "public_geo.wfs: %s returned only already-seen features at "
                    "startIndex %d — paging is not advancing; stopping",
                    source.source_id, offset,
                )
                return

            offset += len(features)
            matched = payload.get("numberMatched", payload.get("totalFeatures"))
            if isinstance(matched, int) and offset >= matched:
                return
            if len(features) < page_size:
                return
