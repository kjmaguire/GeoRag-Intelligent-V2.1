"""Stage 3.5 — Deterministic coordinate extraction from PDF text blocks.

§04p Phase 2.A responsibilities:
  - Extract geographic coordinates from silver.pdf_text_blocks using validated
    regex patterns (UTM, lat/lon decimal, lat/lon DMS).
  - Validate every match against numeric bounds before emitting it.
  - Attach a nearby datum hint (NAD27/NAD83/WGS84) when found within 200 chars
    of the coordinate match in the same text block.
  - Cache results durably in silver.pdf_coordinates so cross-process and
    cross-restart cache hits avoid redundant regex passes.

§04p key-note enforcement
--------------------------
"Determinism over LLM where possible. find_coordinates, assay-value
extraction, and other numeric patterns MUST be implemented as validated
regex/Pydantic extractors before the VL model sees them.  The VL model
does not invent UTM coordinates or ppm values — those are deterministic-
extraction targets."

This module IS that enforcement for coordinates.  The Pydantic AI agent
calls this endpoint first; the VL model only receives the typed, bounds-
checked output — never raw PDF text containing potential coordinate strings.

Threading model — async-only (no process pool)
----------------------------------------------
Regex over a few KB of text per block is fast enough to run directly in
the async event loop (typically < 1 ms per block).  The §04p instruction
confirms: "regex is CPU-bound but fast; doesn't need process isolation".

If a future service-level benchmark shows contention on large PDFs (hundreds
of pages with dense coordinate text), wrap the per-block regex passes in
``asyncio.get_running_loop().run_in_executor(None, ...)`` to move them off
the event loop.  The current implementation defaults to direct synchronous
regex for simplicity.

Match-bbox derivation from char-offset
---------------------------------------
Phase 2.A approximates the match bbox by linear interpolation of the char
offset within the source block's overall bbox (LTR, uniform-width assumption):

    char_fraction_start = match.start() / max(len(text), 1)
    char_fraction_end   = match.end()   / max(len(text), 1)
    match_x0 = block_x0 + (block_x1 - block_x0) * char_fraction_start
    match_x1 = block_x0 + (block_x1 - block_x0) * char_fraction_end
    match_y0 = block_y0    (same as block — single-line approximation)
    match_y1 = block_y1

Limitation: this is a single-row approximation.  Multi-line text blocks
will have the same y-bounds as the full block, and x offsets are less
accurate for proportional fonts.  Phase 2.B can refine using the per-char
bbox data from pdfminer.six's LTChar objects if exact sub-character
positioning is required by the agent.

Cache behaviour
---------------
On a cache hit (rows already present in silver.pdf_coordinates for this
pdf_id + page), the service returns them directly without re-running regex.
The cache is per (pdf_id, page) combination.  Passing page=None processes
all pages and caches each page's results independently.

Duplicate guard: ON CONFLICT (pdf_id, page, raw_match) DO NOTHING.
This means if two concurrent processes extract the same PDF simultaneously
only the first INSERT wins; the second is silently dropped.  The returned
rows will include what was already in the cache.

Lifespan integration
--------------------
PdfCoordinatesService is a singleton held on app.state.pdf_coordinates_service.
Initialise it in the FastAPI lifespan startup hook after the asyncpg pool:

    app.state.pdf_coordinates_service = PdfCoordinatesService(pool=pg_pool)

No teardown required — the service holds no process pool or external clients.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level regex constants
# ---------------------------------------------------------------------------
# All patterns are compiled once at import time (re.IGNORECASE for flexibility
# with mixed-case UTM notation in scanned geological reports).

# --------------------------------------------------------------------------
# UTM — full form:  Zone 12N 480500mE 6055000mN
#   Captures: (zone_digits, hemisphere, easting, northing)
#   "Zone" keyword required; "m" before E/N is optional.
# --------------------------------------------------------------------------
_UTM_PATTERN_FULL = re.compile(
    r"\bZone\s+(\d{1,2})\s*([NS])\s+"        # "Zone 12N" or "Zone 12 N"
    r"(\d{6,7})\s*m?\s*E\s+"                  # easting: 480500 or 480500mE
    r"(\d{7,8})\s*m?\s*N\b",                  # northing: 6055000 or 6055000mN
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# UTM — terse form:  12N 480500 6055000
#   Captures: (zone_digits, hemisphere, easting, northing)
#   Only matched when "UTM" or "Zone" appears within 50 chars of the match
#   position in the source text (proximity heuristic — guards against false
#   positives on bare digit sequences that happen to be 6–7 digits long).
# --------------------------------------------------------------------------
_UTM_PATTERN_TERSE = re.compile(
    r"\b(\d{1,2})([NS])\s+"                   # zone + hemisphere: 12N
    r"(\d{6,7})\s+"                            # easting: 480500
    r"(\d{7,8})\b",                            # northing: 6055000
    re.IGNORECASE,
)

# Guard pattern for the terse UTM proximity heuristic:
# "UTM" or "Zone" (case-insensitive) must appear within 50 chars of the terse match.
_UTM_CONTEXT_GUARD = re.compile(r"\b(UTM|Zone)\b", re.IGNORECASE)

# --------------------------------------------------------------------------
# Lat/Lon decimal:  54.6789°N 110.4321°W   or   54.6789, -110.4321
#   Captures: (lat_value, lat_hemi_or_None, lon_value, lon_hemi_or_None)
#   lat_hemi: N/S or None (sign-coded via leading '-')
#   lon_hemi: E/W or None (sign-coded via leading '-')
# --------------------------------------------------------------------------
_LATLON_DECIMAL_PATTERN = re.compile(
    r"(-?\d{1,3}\.\d+)"                        # latitude (may have leading '-')
    r"\s*°?\s*([NS])?"                          # optional degree symbol + hemisphere
    r"\s*,?\s+"                                 # optional comma separator
    r"(-?\d{1,3}\.\d+)"                         # longitude (may have leading '-')
    r"\s*°?\s*([EW])?",                         # optional degree symbol + hemisphere
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# Lat/Lon DMS:  54°40'44"N 110°25'56"W
#   Captures: (lat_d, lat_m, lat_s, lat_hemi, lon_d, lon_m, lon_s, lon_hemi)
# --------------------------------------------------------------------------
_LATLON_DMS_PATTERN = re.compile(
    r"(\d{1,3})\s*°\s*(\d{1,2})\s*'\s*(\d{1,2}(?:\.\d+)?)\s*\"\s*([NS])"
    r"\s+"
    r"(\d{1,3})\s*°\s*(\d{1,2})\s*'\s*(\d{1,2}(?:\.\d+)?)\s*\"\s*([EW])",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------
# Datum context pattern — used to tag nearby coordinate matches.
# Attached to coord matches within 200 chars of the datum string in the
# same block.  Not a coordinate itself — metadata only.
# --------------------------------------------------------------------------
_DATUM_PATTERN = re.compile(
    r"\b(NAD\s*27|NAD\s*83|WGS\s*84)\b",
    re.IGNORECASE,
)

# Maximum character distance in the block text for datum attachment.
_DATUM_PROXIMITY_CHARS = 200

# ---------------------------------------------------------------------------
# Pydantic validation models (internal — not re-exported from models/pdf.py)
# ---------------------------------------------------------------------------


class _UtmCoord(BaseModel):
    """Validated UTM coordinate — bounds-checked before insertion."""

    utm_zone: int
    utm_hemisphere: str  # 'N' or 'S'
    utm_easting: float
    utm_northing: float

    @field_validator("utm_zone")
    @classmethod
    def _zone_bounds(cls, v: int) -> int:
        if not 1 <= v <= 60:
            raise ValueError(f"utm_zone {v} out of range [1, 60]")
        return v

    @field_validator("utm_hemisphere")
    @classmethod
    def _hemi_upper(cls, v: str) -> str:
        v = v.upper()
        if v not in ("N", "S"):
            raise ValueError(f"utm_hemisphere must be N or S, got {v!r}")
        return v

    @field_validator("utm_easting")
    @classmethod
    def _easting_bounds(cls, v: float) -> float:
        if not 100_000.0 <= v <= 900_000.0:
            raise ValueError(f"utm_easting {v} out of range [100000, 900000]")
        return v

    @field_validator("utm_northing")
    @classmethod
    def _northing_bounds(cls, v: float) -> float:
        if not 0.0 <= v <= 10_000_000.0:
            raise ValueError(f"utm_northing {v} out of range [0, 10000000]")
        return v


class _LatlonCoord(BaseModel):
    """Validated lat/lon coordinate — bounds-checked before insertion."""

    latitude: float
    longitude: float

    @field_validator("latitude")
    @classmethod
    def _lat_bounds(cls, v: float) -> float:
        if not -90.0 <= v <= 90.0:
            raise ValueError(f"latitude {v} out of range [-90, 90]")
        return v

    @field_validator("longitude")
    @classmethod
    def _lon_bounds(cls, v: float) -> float:
        if not -180.0 <= v <= 180.0:
            raise ValueError(f"longitude {v} out of range [-180, 180]")
        return v


# ---------------------------------------------------------------------------
# Helper: DMS → decimal degrees
# ---------------------------------------------------------------------------

def _dms_to_decimal(degrees: float, minutes: float, seconds: float, hemisphere: str) -> float:
    """Convert degrees-minutes-seconds + hemisphere to decimal degrees.

    The hemisphere controls sign inversion:
      S and W hemispheres → negative decimal value.

    >>> _dms_to_decimal(54, 40, 44, 'N')   # ≈ 54.6789
    54.678888...
    >>> _dms_to_decimal(110, 25, 56, 'W')  # ≈ -110.4322
    -110.432222...
    """
    decimal = degrees + minutes / 60.0 + seconds / 3600.0
    if hemisphere.upper() in ("S", "W"):
        decimal = -decimal
    return decimal


# ---------------------------------------------------------------------------
# Helper: find nearest datum string to a match position
# ---------------------------------------------------------------------------

def _find_nearest_datum(text: str, match_start: int, match_end: int) -> str | None:
    """Return the normalised datum string nearest to [match_start, match_end].

    Searches the text for _DATUM_PATTERN matches and returns the datum string
    for the one whose centre is within _DATUM_PROXIMITY_CHARS characters of
    the coordinate match centre.  Returns None when no datum is close enough.

    Normalisation: collapse internal whitespace (e.g. "NAD 83" → "NAD83").
    """
    match_centre = (match_start + match_end) / 2.0
    best_datum: str | None = None
    best_distance = float("inf")

    for m in _DATUM_PATTERN.finditer(text):
        datum_centre = (m.start() + m.end()) / 2.0
        distance = abs(datum_centre - match_centre)
        if distance <= _DATUM_PROXIMITY_CHARS and distance < best_distance:
            best_distance = distance
            # Normalise: collapse internal whitespace, uppercase.
            raw = m.group(1)
            best_datum = re.sub(r"\s+", "", raw).upper()  # "NAD 83" → "NAD83"

    return best_datum


# ---------------------------------------------------------------------------
# Helper: derive match_bbox from block bbox + char offset (linear interpolation)
# ---------------------------------------------------------------------------

def _derive_match_bbox(
    block: dict,
    match_start: int,
    match_end: int,
    text_len: int,
) -> tuple[float, float, float, float] | None:
    """Approximate bbox of a regex match within a text block.

    Uses linear interpolation of char offset within the block's full bbox
    (LTR, uniform-width assumption).  See module docstring for limitations.

    Returns None when block bbox values are unavailable or degenerate
    (x0 >= x1 would produce a zero-width match bbox, which is unhelpful
    and would mislead downstream consumers).
    """
    x0 = block.get("bbox_x0")
    y0 = block.get("bbox_y0")
    x1 = block.get("bbox_x1")
    y1 = block.get("bbox_y1")

    if x0 is None or y0 is None or x1 is None or y1 is None:
        return None
    if x0 >= x1 or text_len == 0:
        return None

    block_width = float(x1) - float(x0)
    frac_start = match_start / text_len
    frac_end = match_end / text_len

    match_x0 = float(x0) + block_width * frac_start
    match_x1 = float(x0) + block_width * frac_end

    return (match_x0, float(y0), match_x1, float(y1))


# ---------------------------------------------------------------------------
# Core extraction logic — called per-block, sync (regex is fast)
# ---------------------------------------------------------------------------

def _extract_from_block(block: dict) -> list[dict]:
    """Extract all coordinate matches from a single text block dict.

    Returns a list of partial coordinate dicts suitable for INSERT into
    silver.pdf_coordinates.  Each dict includes:
        coord_kind, raw_match, match_bbox_{x0,y0,x1,y1},
        latitude, longitude, utm_zone, utm_hemisphere, utm_easting,
        utm_northing, datum.

    Matches that fail Pydantic bounds-checking are discarded silently
    (logged at DEBUG level).  This is the §04p "bad regex matches are bad
    data, never surfaced to downstream" rule.
    """
    text: str = block.get("text", "")
    if not text:
        return []

    text_len = len(text)
    results: list[dict] = []

    # ----------------------------------------------------------------
    # 1. UTM full form (Zone 12N 480500mE 6055000mN)
    # ----------------------------------------------------------------
    for m in _UTM_PATTERN_FULL.finditer(text):
        try:
            coord = _UtmCoord(
                utm_zone=int(m.group(1)),
                utm_hemisphere=m.group(2).upper(),
                utm_easting=float(m.group(3)),
                utm_northing=float(m.group(4)),
            )
        except Exception as exc:
            logger.debug("UTM full-form bounds check failed for %r: %s", m.group(0), exc)
            continue

        bbox = _derive_match_bbox(block, m.start(), m.end(), text_len)
        datum = _find_nearest_datum(text, m.start(), m.end())
        results.append({
            "coord_kind": "utm",
            "raw_match": m.group(0),
            "match_bbox": bbox,
            "latitude": None,
            "longitude": None,
            "utm_zone": coord.utm_zone,
            "utm_hemisphere": coord.utm_hemisphere,
            "utm_easting": coord.utm_easting,
            "utm_northing": coord.utm_northing,
            "datum": datum,
        })

    # ----------------------------------------------------------------
    # 2. UTM terse form (12N 480500 6055000) — proximity guard required
    # ----------------------------------------------------------------
    for m in _UTM_PATTERN_TERSE.finditer(text):
        # Proximity guard: "UTM" or "Zone" must appear within 50 chars.
        window_start = max(0, m.start() - 50)
        window_end = min(text_len, m.end() + 50)
        context_window = text[window_start:window_end]
        if not _UTM_CONTEXT_GUARD.search(context_window):
            logger.debug(
                "UTM terse match %r skipped — no UTM/Zone context within 50 chars",
                m.group(0),
            )
            continue

        try:
            coord = _UtmCoord(
                utm_zone=int(m.group(1)),
                utm_hemisphere=m.group(2).upper(),
                utm_easting=float(m.group(3)),
                utm_northing=float(m.group(4)),
            )
        except Exception as exc:
            logger.debug("UTM terse bounds check failed for %r: %s", m.group(0), exc)
            continue

        # Guard against duplicating a match already captured by the full pattern.
        raw = m.group(0)
        if any(r["raw_match"] == raw for r in results):
            continue

        bbox = _derive_match_bbox(block, m.start(), m.end(), text_len)
        datum = _find_nearest_datum(text, m.start(), m.end())
        results.append({
            "coord_kind": "utm",
            "raw_match": raw,
            "match_bbox": bbox,
            "latitude": None,
            "longitude": None,
            "utm_zone": coord.utm_zone,
            "utm_hemisphere": coord.utm_hemisphere,
            "utm_easting": coord.utm_easting,
            "utm_northing": coord.utm_northing,
            "datum": datum,
        })

    # ----------------------------------------------------------------
    # 3. Lat/lon decimal (54.6789°N 110.4321°W or 54.6789, -110.4321)
    # ----------------------------------------------------------------
    for m in _LATLON_DECIMAL_PATTERN.finditer(text):
        lat_raw = float(m.group(1))
        lat_hemi = (m.group(2) or "").upper()
        lon_raw = float(m.group(3))
        lon_hemi = (m.group(4) or "").upper()

        # Apply hemisphere sign inversion.
        # Explicit hemisphere letter always wins over sign-coded value.
        if lat_hemi == "S":
            latitude = -abs(lat_raw)
        elif lat_hemi == "N":
            latitude = abs(lat_raw)
        else:
            # No hemisphere letter — use the sign as-is.
            latitude = lat_raw

        if lon_hemi == "W":
            longitude = -abs(lon_raw)
        elif lon_hemi == "E":
            longitude = abs(lon_raw)
        else:
            longitude = lon_raw

        try:
            coord = _LatlonCoord(latitude=latitude, longitude=longitude)
        except Exception as exc:
            logger.debug(
                "lat/lon decimal bounds check failed for %r: %s", m.group(0), exc
            )
            continue

        bbox = _derive_match_bbox(block, m.start(), m.end(), text_len)
        datum = _find_nearest_datum(text, m.start(), m.end())
        results.append({
            "coord_kind": "latlon_decimal",
            "raw_match": m.group(0),
            "match_bbox": bbox,
            "latitude": coord.latitude,
            "longitude": coord.longitude,
            "utm_zone": None,
            "utm_hemisphere": None,
            "utm_easting": None,
            "utm_northing": None,
            "datum": datum,
        })

    # ----------------------------------------------------------------
    # 4. Lat/lon DMS (54°40'44"N 110°25'56"W)
    # ----------------------------------------------------------------
    for m in _LATLON_DMS_PATTERN.finditer(text):
        try:
            lat_d = float(m.group(1))
            lat_m = float(m.group(2))
            lat_s = float(m.group(3))
            lat_hemi = m.group(4).upper()
            lon_d = float(m.group(5))
            lon_m = float(m.group(6))
            lon_s = float(m.group(7))
            lon_hemi = m.group(8).upper()

            latitude = _dms_to_decimal(lat_d, lat_m, lat_s, lat_hemi)
            longitude = _dms_to_decimal(lon_d, lon_m, lon_s, lon_hemi)

            coord = _LatlonCoord(latitude=latitude, longitude=longitude)
        except Exception as exc:
            logger.debug(
                "lat/lon DMS bounds check failed for %r: %s", m.group(0), exc
            )
            continue

        bbox = _derive_match_bbox(block, m.start(), m.end(), text_len)
        datum = _find_nearest_datum(text, m.start(), m.end())
        results.append({
            "coord_kind": "latlon_dms",
            "raw_match": m.group(0),
            "match_bbox": bbox,
            "latitude": coord.latitude,
            "longitude": coord.longitude,
            "utm_zone": None,
            "utm_hemisphere": None,
            "utm_easting": None,
            "utm_northing": None,
            "datum": datum,
        })

    return results


# ---------------------------------------------------------------------------
# PdfCoordinatesService singleton
# ---------------------------------------------------------------------------


class PdfCoordinatesService:
    """Phase 2.A coordinate extraction service — singleton on app.state.

    Reads text from silver.pdf_text_blocks (populated by Phase 1.B).
    Runs deterministic regex over each block, validates via Pydantic, and
    caches results in silver.pdf_coordinates.

    The service is async-native (asyncpg) and holds no process pool — regex
    over a few KB of text is fast enough for the async event loop.

    Usage in FastAPI lifespan::

        app.state.pdf_coordinates_service = PdfCoordinatesService(pool=pg_pool)

    Then in route handlers::

        service = request.app.state.pdf_coordinates_service
        coords, cache_hit = await service.find_coordinates(pdf_id, page=1)
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool
        logger.info("PdfCoordinatesService ready (§04p Phase 2.A — deterministic regex)")

    # -----------------------------------------------------------------------
    # Internal: cache check
    # -----------------------------------------------------------------------

    async def _cache_hit(
        self,
        pdf_id: str,
        page: int | None,
        workspace_id: uuid.UUID,
    ) -> list[dict] | None:
        """Return cached rows from silver.pdf_coordinates, or None on miss."""
        async with self._pool.acquire() as conn:
            if page is not None:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM silver.pdf_coordinates"
                    " WHERE workspace_id = $1 AND pdf_id = $2 AND page = $3",
                    workspace_id, pdf_id, page,
                )
            else:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM silver.pdf_coordinates"
                    " WHERE workspace_id = $1 AND pdf_id = $2",
                    workspace_id, pdf_id,
                )

            if not count:
                return None

            if page is not None:
                rows = await conn.fetch(
                    "SELECT coord_id, pdf_id, page, source_block_id,"
                    "       coord_kind, raw_match,"
                    "       match_bbox_x0, match_bbox_y0, match_bbox_x1, match_bbox_y1,"
                    "       latitude, longitude,"
                    "       utm_zone, utm_hemisphere, utm_easting, utm_northing,"
                    "       datum, extraction_confidence, source_method, extracted_at"
                    " FROM silver.pdf_coordinates"
                    " WHERE workspace_id = $1 AND pdf_id = $2 AND page = $3"
                    " ORDER BY page, match_bbox_y1 DESC, match_bbox_x0",
                    workspace_id, pdf_id, page,
                )
            else:
                rows = await conn.fetch(
                    "SELECT coord_id, pdf_id, page, source_block_id,"
                    "       coord_kind, raw_match,"
                    "       match_bbox_x0, match_bbox_y0, match_bbox_x1, match_bbox_y1,"
                    "       latitude, longitude,"
                    "       utm_zone, utm_hemisphere, utm_easting, utm_northing,"
                    "       datum, extraction_confidence, source_method, extracted_at"
                    " FROM silver.pdf_coordinates"
                    " WHERE workspace_id = $1 AND pdf_id = $2"
                    " ORDER BY page, match_bbox_y1 DESC, match_bbox_x0",
                    workspace_id, pdf_id,
                )

        return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Internal: load text blocks from Phase 1.B cache
    # -----------------------------------------------------------------------

    async def _load_text_blocks(
        self,
        pdf_id: str,
        page: int | None,
        workspace_id: uuid.UUID,
    ) -> list[dict]:
        """Load text blocks from silver.pdf_text_blocks.

        Returns an empty list when no blocks are cached (caller should pre-call
        extract_text to populate).
        """
        async with self._pool.acquire() as conn:
            if page is not None:
                rows = await conn.fetch(
                    "SELECT block_id, page, bbox_x0, bbox_y0, bbox_x1, bbox_y1, text"
                    " FROM silver.pdf_text_blocks"
                    " WHERE workspace_id = $1 AND pdf_id = $2 AND page = $3"
                    " ORDER BY bbox_y1 DESC, bbox_x0",
                    workspace_id, pdf_id, page,
                )
            else:
                rows = await conn.fetch(
                    "SELECT block_id, page, bbox_x0, bbox_y0, bbox_x1, bbox_y1, text"
                    " FROM silver.pdf_text_blocks"
                    " WHERE workspace_id = $1 AND pdf_id = $2"
                    " ORDER BY page, bbox_y1 DESC, bbox_x0",
                    workspace_id, pdf_id,
                )
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Internal: persist extracted coordinates
    # -----------------------------------------------------------------------

    async def _persist_coordinates(
        self,
        pdf_id: str,
        workspace_id: uuid.UUID,
        coords: list[dict],
    ) -> None:
        """Bulk-insert extracted coordinates into silver.pdf_coordinates.

        Uses ON CONFLICT (pdf_id, page, raw_match) DO NOTHING to handle
        duplicate/concurrent extraction idempotently.
        """
        if not coords:
            return

        now = datetime.now(tz=UTC)
        records = []
        for c in coords:
            bbox = c.get("match_bbox")
            records.append((
                uuid.uuid4(),                               # coord_id
                workspace_id,                               # workspace_id
                pdf_id,                                     # pdf_id
                c["page"],                                  # page
                c.get("source_block_id"),                   # source_block_id (UUID or None)
                c["coord_kind"],                            # coord_kind
                c["raw_match"],                             # raw_match
                bbox[0] if bbox else None,                  # match_bbox_x0
                bbox[1] if bbox else None,                  # match_bbox_y0
                bbox[2] if bbox else None,                  # match_bbox_x1
                bbox[3] if bbox else None,                  # match_bbox_y1
                c.get("latitude"),                          # latitude
                c.get("longitude"),                         # longitude
                c.get("utm_zone"),                          # utm_zone
                c.get("utm_hemisphere"),                    # utm_hemisphere
                c.get("utm_easting"),                       # utm_easting
                c.get("utm_northing"),                      # utm_northing
                c.get("datum"),                             # datum
                1.0,                                        # extraction_confidence
                "regex",                                    # source_method
                now,                                        # extracted_at
            ))

        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO silver.pdf_coordinates"
                " (coord_id, workspace_id, pdf_id, page, source_block_id,"
                "  coord_kind, raw_match,"
                "  match_bbox_x0, match_bbox_y0, match_bbox_x1, match_bbox_y1,"
                "  latitude, longitude,"
                "  utm_zone, utm_hemisphere, utm_easting, utm_northing,"
                "  datum, extraction_confidence, source_method, extracted_at)"
                " VALUES"
                " ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,"
                "  $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21)"
                " ON CONFLICT (pdf_id, page, raw_match) DO NOTHING",
                records,
            )
        logger.debug(
            "Persisted %d coordinate rows for pdf_id=%s", len(records), pdf_id[:16]
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def find_coordinates(
        self,
        pdf_id: str,
        workspace_id: uuid.UUID,
        page: int | None = None,
    ) -> tuple[list[dict], bool]:
        """Extract and cache geographic coordinates from a PDF's text blocks.

        Parameters
        ----------
        pdf_id:
            SHA-256 hex of the normalised PDF (Bronze archive key).
        workspace_id:
            Tenant workspace UUID. Required — silver.pdf_coordinates.workspace_id
            is NOT NULL, and the cache is scoped per-workspace.
        page:
            1-indexed page to process, or None for all pages.

        Returns
        -------
        (coords, cache_hit)
            coords:    list of coordinate dicts matching the silver.pdf_coordinates
                       column shape.  Empty list when no text blocks are available
                       (caller should pre-call extract_text first).
            cache_hit: True if results came from the Silver cache.

        Notes
        -----
        Empty list + cache_hit=False does NOT mean extraction failed — it means
        either (a) no text blocks are cached yet (call extract_text first) or
        (b) no coordinate patterns were found in the text.

        The endpoint returns 200 + empty coordinates in both cases (NOT 404).
        This is §04p behaviour: the deterministic extractor ran and found nothing.
        404 is reserved for "pdf_id not in Bronze store".
        """
        # Cache check first.
        cached = await self._cache_hit(pdf_id, page, workspace_id)
        if cached is not None:
            logger.debug(
                "find_coordinates cache HIT pdf_id=%s page=%s count=%d",
                pdf_id[:16], page, len(cached),
            )
            return cached, True

        # Cache miss — load text blocks from Phase 1.B.
        blocks = await self._load_text_blocks(pdf_id, page, workspace_id)
        if not blocks:
            logger.debug(
                "find_coordinates: no text blocks for pdf_id=%s page=%s — "
                "call extract_text first to populate silver.pdf_text_blocks",
                pdf_id[:16], page,
            )
            return [], False

        # Run regex extraction over each block.
        all_coords: list[dict] = []
        for block in blocks:
            matches = _extract_from_block(block)
            for match in matches:
                # Attach page and source_block_id from the parent block.
                match["page"] = block["page"]
                match["source_block_id"] = block.get("block_id")
            all_coords.extend(matches)

        # Persist to cache (idempotent — ON CONFLICT DO NOTHING).
        await self._persist_coordinates(pdf_id, workspace_id, all_coords)

        logger.debug(
            "find_coordinates extracted %d coords from %d blocks for pdf_id=%s page=%s",
            len(all_coords), len(blocks), pdf_id[:16], page,
        )
        return all_coords, False
