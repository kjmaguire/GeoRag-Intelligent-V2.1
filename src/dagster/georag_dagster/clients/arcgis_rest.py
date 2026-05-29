"""ArcGIS REST FeatureServer client for Public Geoscience Bronze ingestion.

Government geological surveys routinely publish mineral-exploration feature
layers through Esri ArcGIS REST services. For Saskatchewan (our first active
jurisdiction) the endpoints are at gis.saskatchewan.ca/arcgis/rest/services/…

This module wraps the three capabilities we need from those services:

    1. Read per-service and per-layer metadata (including the upstream's
       own `serviceLastEditDate` / `editingInfo.lastEditDate` — used for the
       Phase-2.3 daily short-circuit check).
    2. Enumerate sub-layers of a FeatureServer (for multi-layer services such
       as Saskatchewan's Resource_Map, where each commodity is its own layer).
    3. Paginate the `/query` endpoint and assemble a single GeoJSON
       FeatureCollection, preserving the source CRS verbatim so the Bronze
       archive is round-trip-able (reprojection lives at Silver tier,
       plan §05b).

Design:

  - Synchronous (`httpx.Client`). Dagster asset execution is sync; any parallel
    fetching is across ASSETS (Dagster orchestration), not inside an asset.
  - Retry on transient 5xx + connection errors with exponential backoff. Never
    retry on 4xx — those indicate a genuine client bug (bad URL, bad params).
  - Returns plain `dict` GeoJSON rather than Pydantic models; FeatureCollection
    shape is well-known and we want to preserve the server's key ordering and
    extension attributes verbatim for audit.
  - Fails loudly. Plan §05a: "No transformation. Bronze is immutable." If
    pagination stalls, or the server returns partial pages, the asset
    should fail and Dagster will surface it — never a silent half-fetch.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import httpx

DEFAULT_PAGE_SIZE = 2000
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 2.0


class ArcGisRestError(RuntimeError):
    """Raised when an ArcGIS REST call fails in a way that should not retry."""


class ArcGisPaginationStalledError(RuntimeError):
    """Raised when the server keeps returning features but never reports
    `exceededTransferLimit = false`, suggesting we're in a loop or missing
    pagination support."""


@dataclass
class LayerMetadata:
    """Subset of ArcGIS layer metadata that matters for Bronze ingestion."""

    layer_id: int
    name: str
    geometry_type: str | None
    capabilities: str | None
    max_record_count: int | None
    source_spatial_reference_wkid: int | None
    last_edit_date_ms: int | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class ServiceMetadata:
    """Subset of ArcGIS FeatureServer-level metadata."""

    name: str | None
    service_last_edit_date_ms: int | None
    layers: list[LayerMetadata]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class FetchResult:
    """One complete, paginated fetch of a single layer."""

    feature_collection: dict[str, Any]
    feature_count: int
    spatial_reference_wkid: int | None
    layer_last_edit_date_ms: int | None
    service_last_edit_date_ms: int | None
    query_params: dict[str, Any]
    response_headers: dict[str, str]
    pages_fetched: int


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_service_metadata(
    service_url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ServiceMetadata:
    """Fetch FeatureServer-level metadata.

    `service_url` should be the FeatureServer root — e.g.
    `.../Resource_Map/FeatureServer` (no trailing layer index). If the URL
    already points at a specific layer, we strip the trailing numeric segment
    before asking for service metadata.
    """
    root = _strip_layer_index(service_url)
    url = f"{root}?f=json"
    data = _get_json(url, timeout=timeout)

    layer_dicts = data.get("layers", []) or []
    layers: list[LayerMetadata] = []
    for layer in layer_dicts:
        # Service-level layer listing is thin — a follow-up GET on the
        # individual layer is required for full metadata. For Bronze we only
        # need name + id here; callers use fetch_layer_metadata() if they
        # need geometry_type / last_edit_date.
        layers.append(
            LayerMetadata(
                layer_id=int(layer.get("id", -1)),
                name=str(layer.get("name", "")),
                geometry_type=layer.get("geometryType"),
                capabilities=None,
                max_record_count=None,
                source_spatial_reference_wkid=None,
                last_edit_date_ms=None,
                raw=layer,
            )
        )

    editing_info = data.get("editingInfo") or {}

    return ServiceMetadata(
        name=data.get("name"),
        service_last_edit_date_ms=editing_info.get("lastEditDate"),
        layers=layers,
        raw=data,
    )


def fetch_layer_metadata(
    layer_url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> LayerMetadata:
    """Fetch per-layer metadata (geometryType, capabilities, editingInfo, CRS)."""
    url = f"{layer_url}?f=json"
    data = _get_json(url, timeout=timeout)

    editing_info = data.get("editingInfo") or {}
    source_sr = data.get("sourceSpatialReference") or data.get("extent", {}).get("spatialReference") or {}

    return LayerMetadata(
        layer_id=int(data.get("id", -1)),
        name=str(data.get("name", "")),
        geometry_type=data.get("geometryType"),
        capabilities=data.get("capabilities"),
        max_record_count=data.get("maxRecordCount"),
        source_spatial_reference_wkid=source_sr.get("wkid") or source_sr.get("latestWkid"),
        last_edit_date_ms=editing_info.get("lastEditDate"),
        raw=data,
    )


def fetch_layer_geojson(
    layer_url: str,
    *,
    source_wkid: int | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    where_clause: str = "1=1",
) -> FetchResult:
    """Paginate the `/query` endpoint and assemble a single FeatureCollection.

    - `layer_url`: full layer URL, e.g. `.../FeatureServer/2`. `/query` is
      appended internally.
    - `source_wkid`: EPSG code to ask the server to return coordinates in.
      If None, the server's native CRS is used. We pass this explicitly so
      Bronze preserves the declared `source_crs` from the registry.
    - `page_size`: ArcGIS `resultRecordCount`. Most Saskatchewan services
      cap at 2000; larger requests are silently truncated.
    - `where_clause`: default `1=1` (all features). Kept as a parameter for
      future filtering (e.g. commodity-scoped pulls).
    """
    query_url = f"{layer_url.rstrip('/')}/query"

    # Fetch layer metadata up-front. We stash service_last_edit_date on the
    # FetchResult so the sidecar manifest (and the Phase-2.3 short-circuit)
    # has it without needing a second RTT from the asset body.
    layer_meta = fetch_layer_metadata(layer_url, timeout=timeout)
    service_meta = fetch_service_metadata(layer_url, timeout=timeout)

    base_params: dict[str, Any] = {
        "where": where_clause,
        "outFields": "*",
        "returnGeometry": "true",
        "f": "geojson",
        "resultRecordCount": int(page_size),
    }
    if source_wkid is not None:
        # `outSR` tells the server what CRS to emit. We want the NATIVE CRS so
        # reprojection is done deterministically at Silver tier (plan §05b).
        base_params["outSR"] = int(source_wkid)

    features: list[dict[str, Any]] = []
    first_response_headers: dict[str, str] = {}
    first_sr_wkid: int | None = None
    pages = 0
    offset = 0
    # Safety rail — a misbehaving server could keep returning
    # `exceededTransferLimit=True` with zero new features, forever.
    max_pages = 10_000

    while pages < max_pages:
        params = dict(base_params, resultOffset=offset)
        resp = _get_with_retry(query_url, params=params, timeout=timeout)
        pages += 1

        if pages == 1:
            first_response_headers = _safe_headers(resp.headers)

        data = resp.json()
        if data.get("error"):
            raise ArcGisRestError(
                f"ArcGIS REST error on page {pages}: {data['error']!r}"
            )

        page_features = data.get("features") or []
        if pages == 1:
            crs = (data.get("crs") or {}).get("properties") or {}
            # GeoJSON `crs` uses `urn:ogc:def:crs:EPSG::XXXX`
            first_sr_wkid = _extract_wkid_from_crs(crs) or source_wkid

        features.extend(page_features)

        got = len(page_features)
        if got == 0:
            # No more features — we're done.
            break

        # ArcGIS signals more data available via `exceededTransferLimit: true`
        # at the FeatureCollection level. Absent that flag, short pages also
        # indicate completion.
        if not data.get("exceededTransferLimit", False) and got < page_size:
            break

        if got < page_size and not data.get("exceededTransferLimit", False):
            break

        offset += got

    else:
        raise ArcGisPaginationStalledError(
            f"ArcGIS pagination exceeded {max_pages} pages at {query_url}; "
            f"server may not support pagination correctly."
        )

    feature_collection = {
        "type": "FeatureCollection",
        "features": features,
    }
    if first_sr_wkid is not None:
        feature_collection["crs"] = {
            "type": "name",
            "properties": {"name": f"urn:ogc:def:crs:EPSG::{first_sr_wkid}"},
        }

    return FetchResult(
        feature_collection=feature_collection,
        feature_count=len(features),
        spatial_reference_wkid=first_sr_wkid,
        layer_last_edit_date_ms=layer_meta.last_edit_date_ms,
        service_last_edit_date_ms=service_meta.service_last_edit_date_ms,
        query_params=base_params,
        response_headers=first_response_headers,
        pages_fetched=pages,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, *, timeout: float) -> dict[str, Any]:
    resp = _get_with_retry(url, timeout=timeout)
    data = resp.json()
    if not isinstance(data, dict):
        raise ArcGisRestError(f"Expected JSON object from {url}, got {type(data).__name__}")
    if data.get("error"):
        raise ArcGisRestError(f"ArcGIS REST error at {url}: {data['error']!r}")
    return data


def _get_with_retry(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF_SECONDS,
) -> httpx.Response:
    """GET with retry on 5xx and network errors. 4xx is fatal.

    One `httpx.Client` is instantiated per call and reused across every
    retry attempt — this pools the TCP/TLS handshake across retries
    instead of reopening on each attempt, which previously made a 3-retry
    failure case fire three full handshakes against Saskatchewan's
    gis.saskatchewan.ca (measurable on large SK feature sets).
    """
    last_exc: Exception | None = None
    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": "GeoRAG/1.0 PublicGeoscienceBronze"},
    ) as client:
        for attempt in range(retries + 1):
            try:
                resp = client.get(url, params=params)

                if 500 <= resp.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"Upstream 5xx at {resp.url}: {resp.status_code}",
                        request=resp.request,
                        response=resp,
                    )
                # 4xx — client bug. raise_for_status() raises
                # httpx.HTTPStatusError, which we catch below and translate
                # to a NON-RETRYABLE ArcGisRestError so a real config bug
                # (bad URL, missing layer, blocked IP) doesn't burn the
                # exponential-backoff budget before failing.
                resp.raise_for_status()
                return resp

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if 400 <= status < 500:
                    # 4xx = fatal. Raise directly without further retries.
                    raise ArcGisRestError(
                        f"Non-retryable {status} from {url}: {exc!r}"
                    ) from exc
                # 5xx — fall through to retry path.
                last_exc = exc
                if attempt >= retries:
                    break
                time.sleep(backoff * (2 ** attempt))

            except httpx.TransportError as exc:
                # Network errors (timeout, connection refused, DNS) — retry.
                last_exc = exc
                if attempt >= retries:
                    break
                time.sleep(backoff * (2 ** attempt))

    raise ArcGisRestError(
        f"Giving up on {url} after {retries + 1} attempts: {last_exc!r}"
    ) from last_exc


def _strip_layer_index(service_url: str) -> str:
    """If `service_url` ends in `/N` (numeric layer index), strip it.

    Idempotent — returns the FeatureServer root URL suitable for `?f=json`
    service metadata calls.
    """
    trimmed = service_url.rstrip("/")
    parts = trimmed.rsplit("/", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return trimmed


def _extract_wkid_from_crs(crs_properties: dict[str, Any]) -> int | None:
    name = str(crs_properties.get("name") or "")
    # e.g. 'urn:ogc:def:crs:EPSG::2957' or 'EPSG:2957'
    if "EPSG" in name:
        tail = name.rsplit(":", 1)[-1]
        if tail.isdigit():
            return int(tail)
    return None


def _safe_headers(headers: httpx.Headers) -> dict[str, str]:
    """Pick a small, audit-useful subset of response headers.

    Avoid stashing cookies, Set-Cookie, or auth-related headers in the Bronze
    manifest.
    """
    allowed = {
        "content-type",
        "content-length",
        "date",
        "etag",
        "last-modified",
        "server",
        "x-request-id",
    }
    return {k: v for k, v in headers.items() if k.lower() in allowed}


def iter_layers(meta: ServiceMetadata) -> Iterator[tuple[int, str]]:
    """Yield (layer_id, name) pairs for each sub-layer of a FeatureServer."""
    for layer in meta.layers:
        yield layer.layer_id, layer.name
