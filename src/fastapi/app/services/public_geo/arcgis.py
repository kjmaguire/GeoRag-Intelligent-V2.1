"""ArcGIS REST client for public geoscience.

Every call here goes to a provincial or federal survey's own service and
returns what it is serving right now. This module is transport only — it does
not decide what to keep or where to put it. ``services.public_geo.sync`` is
what maps and persists; see ``registry`` for the addressing.

Four access patterns, which is all the callers need:

  ``iter_all_features`` paged full-layer walk, for the sync job
  ``query_features``    spatial + attribute filtering, for ad-hoc live lookups
  ``fetch_feature``     one feature by OBJECTID, for citation resolution
  ``count_features``    a cheap COUNT, for "how much is out there" affordances

Shape notes that cost time if you rediscover them:

  - ``f=geojson`` makes the service reproject and emit WGS84 when we pass
    ``outSR=4326``, so callers never deal with the native CRS (2957 for most
    of Saskatchewan, 3005 for BC). The stored pipeline preserved native CRS
    and converted later; live, there is no reason to.
  - Field names differ per layer, so nothing here assumes a schema. Attribute
    filtering builds a ``where`` clause from caller-supplied column names, and
    ``_first_present`` picks a display name from a candidate list.
  - ArcGIS returns HTTP 200 with an ``error`` object in the body on failure.
    Checking status alone silently yields zero features, so the body is
    inspected too.
  - Services cap ``resultRecordCount`` server-side (commonly 1000-2000) and
    simply return fewer rows than asked rather than erroring, so the bulk
    walker advances by what it actually received. ``query_features`` pages
    much smaller on purpose — it serves interactive callers, not exports.

Every function degrades to empty/None rather than raising, matching the
convention in ``app.agent.tools`` — one unreachable survey must not fail a
whole answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.services.public_geo.registry import PublicGeoSource

logger = logging.getLogger(__name__)

# Interactive budget. A survey that cannot answer within this is treated as
# unavailable; the caller shows the others rather than blocking on it.
DEFAULT_TIMEOUT_S = 8.0

# Per-layer page size. Deliberately modest — see module docstring.
DEFAULT_LIMIT = 25
MAX_LIMIT = 200


def _query_url(source: PublicGeoSource) -> str:
    """Resolve a source's `/query` endpoint.

    Most registry rows already carry the layer index in ``service_url``. A few
    point at a MapServer root and carry ``layer_index`` separately; append it
    so both shapes resolve to a queryable layer.
    """
    base = source.service_url.rstrip("/")
    tail = base.rsplit("/", 1)[-1]
    if not tail.isdigit() and source.layer_index is not None:
        base = f"{base}/{source.layer_index}"
    return f"{base}/query"


def _check_arcgis_body(payload: dict[str, Any], *, source_id: str) -> bool:
    """ArcGIS signals failure in-band with HTTP 200. Return True if usable."""
    err = payload.get("error")
    if err:
        logger.warning(
            "public_geo: %s returned an ArcGIS error: %s %s",
            source_id,
            err.get("code"),
            (err.get("message") or "")[:200],
        )
        return False
    return True


def _bbox_params(bbox: tuple[float, float, float, float] | list[float]) -> dict[str, Any]:
    """Envelope intersect filter, expressed in WGS84."""
    min_lon, min_lat, max_lon, max_lat = list(bbox)[:4]
    return {
        "geometry": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
    }


def _where_clause(
    *,
    text: str | None,
    text_fields: list[str] | None,
    extra: str | None,
) -> str:
    """Build a SQL-ish ArcGIS `where`, defaulting to match-everything.

    Text search is a case-insensitive LIKE across whichever fields the caller
    says are name-bearing for this layer. That is genuinely weaker than the
    semantic search the embedded corpus offered, and it is the honest cost of
    not indexing — stated here so nobody mistakes it for a bug.
    """
    clauses: list[str] = []

    if text and text_fields:
        safe = text.replace("'", "''")
        ors = [f"UPPER({f}) LIKE UPPER('%{safe}%')" for f in text_fields]
        clauses.append("(" + " OR ".join(ors) + ")")

    if extra:
        clauses.append(f"({extra})")

    return " AND ".join(clauses) if clauses else "1=1"


async def _get_json(
    url: str,
    params: dict[str, Any],
    *,
    timeout_s: float,
    source_id: str,
) -> dict[str, Any] | None:
    import httpx  # noqa: PLC0415

    def _do() -> dict[str, Any] | None:
        try:
            resp = httpx.get(url, params=params, timeout=timeout_s)
            resp.raise_for_status()
            body: dict[str, Any] = resp.json()
            return body
        except Exception as exc:  # noqa: BLE001 — degrade, never propagate
            logger.warning("public_geo: %s request failed: %s", source_id, exc)
            return None

    payload = await asyncio.to_thread(_do)
    if payload is None:
        return None
    if not _check_arcgis_body(payload, source_id=source_id):
        return None
    return payload


# Layer field names, cached per process. Introspection is one extra request
# the first time a layer is touched; without it a WHERE naming a column the
# layer lacks makes ArcGIS reject the ENTIRE query with a 400, so a text search
# silently returns nothing (observed on CA-SK-MINE-LOC, which has NAME but
# neither MINE_NAME nor SITE_NAME).
_FIELD_CACHE: dict[str, set[str]] = {}


async def layer_fields(
    source: PublicGeoSource, *, timeout_s: float = DEFAULT_TIMEOUT_S
) -> set[str]:
    """Field names this layer exposes, lowercased. Empty set if unknown."""
    if source.source_id in _FIELD_CACHE:
        return _FIELD_CACHE[source.source_id]

    meta_url = _query_url(source).rsplit("/query", 1)[0]
    payload = await _get_json(
        meta_url, {"f": "json"}, timeout_s=timeout_s, source_id=source.source_id
    )
    fields: set[str] = set()
    if payload:
        for f in payload.get("fields") or []:
            nm = f.get("name")
            if nm:
                fields.add(str(nm).lower())

    # Cache even an empty result: a layer that will not describe itself will
    # not do so on retry either, and callers treat empty as "skip text filter".
    _FIELD_CACHE[source.source_id] = fields
    return fields


async def query_features(
    source: PublicGeoSource,
    *,
    text: str | None = None,
    text_fields: list[str] | None = None,
    where_extra: str | None = None,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    limit: int = DEFAULT_LIMIT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> list[dict[str, Any]]:
    """Return GeoJSON features from one live layer.

    Always reprojects to WGS84 (``outSR=4326``) so callers never handle the
    native CRS.
    """
    # Narrow the caller's candidate name columns to the ones this layer really
    # has. Passing a non-existent column is not a soft failure in ArcGIS — it
    # 400s the whole request.
    usable_text_fields: list[str] | None = None
    if text and text_fields:
        present = await layer_fields(source, timeout_s=timeout_s)
        if present:
            usable_text_fields = [f for f in text_fields if f.lower() in present]
            if not usable_text_fields:
                logger.info(
                    "public_geo: %s exposes none of %s — running unfiltered and "
                    "letting the caller filter",
                    source.source_id, text_fields,
                )
        else:
            # Could not introspect; safer to drop the filter than to 400.
            usable_text_fields = None

    params: dict[str, Any] = {
        "where": _where_clause(
            text=text, text_fields=usable_text_fields, extra=where_extra
        ),
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": 4326,
        "f": "geojson",
        "resultRecordCount": max(1, min(int(limit), MAX_LIMIT)),
    }
    if bbox:
        params.update(_bbox_params(bbox))

    payload = await _get_json(
        _query_url(source), params, timeout_s=timeout_s, source_id=source.source_id
    )
    if not payload:
        return []
    return list(payload.get("features") or [])


async def iter_all_features(
    source: PublicGeoSource,
    *,
    page_size: int = 1000,
    max_features: int | None = None,
    timeout_s: float = 60.0,
) -> AsyncIterator[dict[str, Any]]:
    """Yield every feature in a layer, paging until exhausted.

    This is the BULK path, used by the sync job — distinct from
    ``query_features``, which is deliberately capped for interactive use.

    Paging uses ``resultOffset``/``resultRecordCount``. Two failure modes are
    guarded because both are silent otherwise:

      - Services cap ``resultRecordCount`` server-side (commonly 1000-2000)
        and simply return fewer rows than asked, so the loop advances by what
        it actually received rather than by ``page_size``.
      - A service that ignores ``resultOffset`` entirely would return page 1
        forever. Tracking seen OBJECTIDs turns that into a clean stop with a
        warning instead of an infinite loop writing duplicates.

    A longer default timeout than the interactive path: bulk pages are large
    and this runs on a schedule, not in a user's request.
    """
    offset = 0
    seen: set[str] = set()
    yielded = 0

    while True:
        params: dict[str, Any] = {
            "where": "1=1",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": 4326,
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": int(page_size),
        }
        payload = await _get_json(
            _query_url(source), params, timeout_s=timeout_s, source_id=source.source_id
        )
        if not payload:
            return

        features = list(payload.get("features") or [])
        if not features:
            return

        fresh = 0
        for f in features:
            oid = object_id_of(f)
            key = oid or f"{offset}:{fresh}"
            if key in seen:
                continue
            seen.add(key)
            fresh += 1
            yielded += 1
            yield f
            if max_features is not None and yielded >= max_features:
                return

        if fresh == 0:
            logger.warning(
                "public_geo: %s returned only already-seen features at offset %d "
                "— the service is likely ignoring resultOffset; stopping",
                source.source_id, offset,
            )
            return

        # Advance by what the service actually gave us, not what we asked for.
        offset += len(features)

        if payload.get("properties", {}).get("exceededTransferLimit") is False:
            return


async def fetch_feature(
    source: PublicGeoSource,
    object_id: str | int,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any] | None:
    """Fetch a single feature by OBJECTID — the citation-resolution path.

    Citations carry `source_id` + the upstream OBJECTID, which is stable for
    the lifetime of a published feature. If the survey has since deleted or
    renumbered it this returns None, and the caller surfaces the citation as
    unresolvable rather than inventing a record. That is the accepted
    trade-off of resolving live instead of against a stored copy.
    """
    params = {
        "objectIds": str(object_id),
        "outFields": "*",
        "returnGeometry": "true",
        "outSR": 4326,
        "f": "geojson",
    }
    payload = await _get_json(
        _query_url(source), params, timeout_s=timeout_s, source_id=source.source_id
    )
    if not payload:
        return None
    features = list(payload.get("features") or [])
    return features[0] if features else None


async def count_features(
    source: PublicGeoSource,
    *,
    bbox: tuple[float, float, float, float] | list[float] | None = None,
    where_extra: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> int | None:
    """Cheap count. Returns None when the service will not answer."""
    params: dict[str, Any] = {
        "where": _where_clause(text=None, text_fields=None, extra=where_extra),
        "returnCountOnly": "true",
        "f": "json",
    }
    if bbox:
        params.update(_bbox_params(bbox))

    payload = await _get_json(
        _query_url(source), params, timeout_s=timeout_s, source_id=source.source_id
    )
    if not payload:
        return None
    count = payload.get("count")
    return int(count) if isinstance(count, int) else None


def first_present(props: dict[str, Any], candidates: list[str]) -> str | None:
    """First non-empty value among candidate field names, case-insensitively.

    Layers disagree on what the name column is called (NAME, MINE_NAME,
    DEPOSIT_NAME, SHOWING_NAME...). Callers pass a candidate list per
    canonical type rather than this module pretending to know a schema.

    Values are stripped. Several layers publish fixed-width CHAR columns and
    return them space-padded — CA-BC-MINFILE's COMMODITY_DESCRIPTION1..8 come
    back as ``"Uranium                       "`` — which otherwise propagates
    into stored commodity arrays, breaks equality matching against "uranium",
    and renders with a trailing gutter in every citation.
    """
    lowered = {str(k).lower(): v for k, v in props.items()}
    for c in candidates:
        v = lowered.get(c.lower())
        if v is None:
            continue
        text = str(v).strip()
        if text:
            return text
    return None


def object_id_of(feature: dict[str, Any]) -> str | None:
    """Pull the OBJECTID out of a GeoJSON feature.

    GeoJSON from ArcGIS puts it in `id` on the feature, but not every service
    does, so fall back to the usual property spellings.
    """
    if feature.get("id") not in (None, ""):
        return str(feature["id"])
    props = feature.get("properties") or {}
    return first_present(props, ["OBJECTID", "objectid", "FID", "OID"])
