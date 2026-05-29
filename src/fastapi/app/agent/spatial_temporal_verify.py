"""Track A.2 Phase 5 — spatial/temporal claim verification helpers.

Per `docs/plans/track-a2-agentic-retrieval.md` D7 (minimal V1 surface).

Provides pure-ish, deterministic verification of spatial and temporal claims
found in LLM-generated responses.  All signal extraction is regex-based — no
NER, no LLM calls.  PostGIS queries are async via asyncpg; the pool is passed
in from AgentDeps so no new compose services are required.

Hallucination prevention role
------------------------------
Phase 5 adds two siblings to §04i:
  • Spatial grounding  — checks that cited spatial relationships (distances,
    directions, proximities) are consistent with the PostGIS geometry recorded
    for the source chunk's Silver row.
  • Temporal grounding — checks that cited date assertions are consistent with
    the source document's published_at / effective_date / report_date.

Both verifiers return a three-way status:
  consistent    — evidence actively supports the claim
  inconsistent  — evidence actively contradicts the claim (hard refusal signal)
  indeterminate — no authoritative data available; NOT a refusal trigger

Defensive contract
-------------------
Every public helper is wrapped in try/except.  Any exception logs + returns
indeterminate rather than propagating.  Phase 5 MUST NOT block a response.

Timeout
--------
PostGIS queries use the §06e 5 s PostGIS budget.  Both queries are fast point
lookups, not full-table scans.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models.decomposition import (
        ClaimSpatialVerification,
        ClaimTemporalVerification,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level regex packs — compiled once at import time (D7 deterministic)
# ---------------------------------------------------------------------------

_SPATIAL_NUMERIC_PATTERN: re.Pattern[str] = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(km|kilometers?|metres?|meters?|miles?|mi)\b",
    re.IGNORECASE,
)
"""Match numeric distance assertions like '2.5 km', '500 metres', '1.2 miles'."""

_SPATIAL_DIRECTION_PATTERN: re.Pattern[str] = re.compile(
    r"\b(north|south|east|west|northeast|northwest|southeast|southwest|NE|NW|SE|SW)\s+of\b",
    re.IGNORECASE,
)
"""Match cardinal/ordinal direction assertions like 'northeast of', 'SW of'."""

_SPATIAL_PROXIMITY_PATTERN: re.Pattern[str] = re.compile(
    r"\b(near|adjacent\s+to|within|inside|outside)\s+",
    re.IGNORECASE,
)
"""Match proximity assertions like 'near the boundary', 'within the zone'."""

_SPATIAL_COORDINATE_PATTERN: re.Pattern[str] = re.compile(
    r"(?<!\d)(-?\d{1,3}\.\d{3,})[,\s]+(-?\d{1,3}\.\d{3,})(?!\d)",
)
"""Match lat/lon-looking coordinate pairs like '-122.419, 37.774' (best-effort).

Uses negative lookbehind/lookahead instead of \\b so that a leading minus sign
is included in the overall match (\\b cannot anchor before '-').
"""

_TEMPORAL_YEAR_PATTERN: re.Pattern[str] = re.compile(
    r"\b((?:19|20)\d{2})\b",
)
"""Match bare 4-digit years in the 1900–2099 range."""

_TEMPORAL_RANGE_PATTERN: re.Pattern[str] = re.compile(
    r"\bbetween\s+((?:19|20)\d{2})\s+and\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
"""Match date-range assertions like 'between 2019 and 2023'."""

_TEMPORAL_RELATIVE_PRIOR_PATTERN: re.Pattern[str] = re.compile(
    r"\b(prior\s+to|before)\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
"""Match 'prior to YYYY' / 'before YYYY' assertions."""

_TEMPORAL_RELATIVE_AFTER_PATTERN: re.Pattern[str] = re.compile(
    r"\b(since|after)\s+((?:19|20)\d{2})\b",
    re.IGNORECASE,
)
"""Match 'since YYYY' / 'after YYYY' assertions."""


# ---------------------------------------------------------------------------
# Signal dataclasses — lightweight, no Pydantic overhead for internal use
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpatialSignal:
    """A single spatial signal extracted from a claim string."""

    signal_type: str  # 'numeric_distance' | 'direction' | 'proximity' | 'coordinate'
    matched_text: str
    numeric_value_m: float | None = None  # normalised to metres when applicable


@dataclass(frozen=True)
class TemporalSignal:
    """A single temporal signal extracted from a claim string."""

    signal_type: str  # 'year' | 'date_range' | 'relative_prior' | 'relative_after'
    matched_text: str
    year_start: int | None = None
    year_end: int | None = None  # same as year_start for single-year signals


# ---------------------------------------------------------------------------
# Signal extraction — pure functions, no I/O
# ---------------------------------------------------------------------------

_UNIT_TO_M: dict[str, float] = {
    "km": 1_000.0,
    "kilometer": 1_000.0,
    "kilometers": 1_000.0,
    "kilometre": 1_000.0,
    "kilometres": 1_000.0,
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "metre": 1.0,
    "metres": 1.0,
    "mile": 1_609.344,
    "miles": 1_609.344,
    "mi": 1_609.344,
}


def extract_spatial_signals(claim_text: str) -> list[SpatialSignal]:
    """Extract all spatial signals from a claim string.

    Runs four regex patterns in order.  Each match produces one SpatialSignal.
    Returns an empty list if no signals are found.  Never raises.

    Args:
        claim_text: Raw claim text as generated by the LLM.

    Returns:
        list[SpatialSignal] — one entry per regex match across all four patterns.
    """
    signals: list[SpatialSignal] = []
    try:
        for m in _SPATIAL_NUMERIC_PATTERN.finditer(claim_text):
            unit = m.group(2).lower().rstrip(".")
            factor = _UNIT_TO_M.get(unit, 1.0)
            try:
                numeric_m = float(m.group(1)) * factor
            except ValueError:
                numeric_m = None
            signals.append(
                SpatialSignal(
                    signal_type="numeric_distance",
                    matched_text=m.group(0),
                    numeric_value_m=numeric_m,
                )
            )
        for m in _SPATIAL_DIRECTION_PATTERN.finditer(claim_text):
            signals.append(
                SpatialSignal(signal_type="direction", matched_text=m.group(0))
            )
        for m in _SPATIAL_PROXIMITY_PATTERN.finditer(claim_text):
            signals.append(
                SpatialSignal(signal_type="proximity", matched_text=m.group(0))
            )
        for m in _SPATIAL_COORDINATE_PATTERN.finditer(claim_text):
            signals.append(
                SpatialSignal(signal_type="coordinate", matched_text=m.group(0))
            )
    except Exception:
        logger.debug("extract_spatial_signals: regex error (non-fatal)", exc_info=True)
    return signals


def extract_temporal_signals(claim_text: str) -> list[TemporalSignal]:
    """Extract all temporal signals from a claim string.

    Runs four regex patterns in order.  Range / relative patterns consume the
    year tokens they match, reducing duplicate signals.  Returns an empty list
    if no signals are found.  Never raises.

    Args:
        claim_text: Raw claim text as generated by the LLM.

    Returns:
        list[TemporalSignal] — one entry per regex match across all four patterns.
    """
    signals: list[TemporalSignal] = []
    try:
        consumed_spans: list[tuple[int, int]] = []

        for m in _TEMPORAL_RANGE_PATTERN.finditer(claim_text):
            signals.append(
                TemporalSignal(
                    signal_type="date_range",
                    matched_text=m.group(0),
                    year_start=int(m.group(1)),
                    year_end=int(m.group(2)),
                )
            )
            consumed_spans.append(m.span())

        for m in _TEMPORAL_RELATIVE_PRIOR_PATTERN.finditer(claim_text):
            signals.append(
                TemporalSignal(
                    signal_type="relative_prior",
                    matched_text=m.group(0),
                    year_start=int(m.group(2)),
                    year_end=int(m.group(2)),
                )
            )
            consumed_spans.append(m.span())

        for m in _TEMPORAL_RELATIVE_AFTER_PATTERN.finditer(claim_text):
            signals.append(
                TemporalSignal(
                    signal_type="relative_after",
                    matched_text=m.group(0),
                    year_start=int(m.group(2)),
                    year_end=int(m.group(2)),
                )
            )
            consumed_spans.append(m.span())

        # Bare-year pattern — skip positions already consumed by range/relative.
        for m in _TEMPORAL_YEAR_PATTERN.finditer(claim_text):
            start, end = m.span()
            overlaps = any(cs <= start and end <= ce for cs, ce in consumed_spans)
            if not overlaps:
                year = int(m.group(1))
                signals.append(
                    TemporalSignal(
                        signal_type="year",
                        matched_text=m.group(0),
                        year_start=year,
                        year_end=year,
                    )
                )

    except Exception:
        logger.debug("extract_temporal_signals: regex error (non-fatal)", exc_info=True)
    return signals


# ---------------------------------------------------------------------------
# PostGIS spatial verification helper
# ---------------------------------------------------------------------------

_POSTGIS_TIMEOUT_S: float = 5.0
"""§06e PostGIS query timeout budget."""


async def verify_spatial_claim(
    claim: Any,  # ClaimVerification from decomposition
    focus: dict[str, Any] | None,
    pg_pool: Any,  # asyncpg Pool
) -> ClaimSpatialVerification:
    """Verify a spatial claim against the conversation's spatial_focus.

    Pulls the cited source chunk's geometry from silver.* (if available) and
    computes ST_Distance against the spatial_focus centroid / bbox.

    Status semantics:
      consistent    — distance is within ±20% of the claimed numeric distance,
                      OR the spatial focus itself is within the claimed proximity
      inconsistent  — the computed distance contradicts the claim by >20%
      indeterminate — no geometry available OR no spatial focus OR pg_pool absent

    This function never raises.  Any exception → indeterminate + log.

    Args:
        claim:    ClaimVerification instance (uses claim.claim_text to extract signals).
        focus:    ConversationState.spatial_focus dict, or None.
        pg_pool:  asyncpg Pool for PostGIS query.

    Returns:
        ClaimSpatialVerification with status, distance_m, and focus_summary.
    """
    from app.models.decomposition import ClaimSpatialVerification  # noqa: PLC0415

    claim_text: str = getattr(claim, "claim_text", "") or ""
    focus_summary: str = _summarise_spatial_focus(focus)

    try:
        signals = extract_spatial_signals(claim_text)
        if not signals:
            return ClaimSpatialVerification(
                claim_text=claim_text,
                status="indeterminate",
                distance_m=None,
                focus_summary=focus_summary,
            )

        if focus is None or pg_pool is None:
            return ClaimSpatialVerification(
                claim_text=claim_text,
                status="indeterminate",
                distance_m=None,
                focus_summary=focus_summary,
            )

        # Extract focus centroid (lon, lat) from bbox or centroid shape.
        focus_lon, focus_lat = _focus_centroid(focus)
        if focus_lon is None or focus_lat is None:
            return ClaimSpatialVerification(
                claim_text=claim_text,
                status="indeterminate",
                distance_m=None,
                focus_summary=focus_summary,
            )

        # Phase 6.A — read source_chunk_id from the real ClaimVerification field
        # (populated by _build_claim_verifications since Phase 6.A).  Falls back
        # to empty string so the startswith("silver:collars:") guard below is a
        # no-op on any row that predates the 6.A wire-up.
        source_chunk_id: str = getattr(claim, "source_chunk_id", None) or ""
        computed_distance_m: float | None = None

        if source_chunk_id and source_chunk_id.startswith("silver:collars:"):
            # Attempt to resolve the collar's geometry from Silver.
            collar_id = source_chunk_id.split(":")[-1]
            computed_distance_m = await _fetch_collar_distance(
                pg_pool, collar_id, focus_lon, focus_lat
            )

        # If no geometry resolved, fall back to indeterminate.
        if computed_distance_m is None:
            return ClaimSpatialVerification(
                claim_text=claim_text,
                status="indeterminate",
                distance_m=None,
                focus_summary=focus_summary,
            )

        # Compare computed distance against any claimed numeric distance.
        numeric_signals = [s for s in signals if s.signal_type == "numeric_distance" and s.numeric_value_m]
        if not numeric_signals:
            # Direction/proximity signal but no numeric threshold — indeterminate.
            return ClaimSpatialVerification(
                claim_text=claim_text,
                status="indeterminate",
                distance_m=computed_distance_m,
                focus_summary=focus_summary,
            )

        # Evaluate against the first numeric signal found.
        claimed_m = numeric_signals[0].numeric_value_m  # type: ignore[assignment]
        tolerance = claimed_m * 0.20  # ±20 % tolerance
        if abs(computed_distance_m - claimed_m) <= tolerance:
            status = "consistent"
        else:
            status = "inconsistent"
            logger.info(
                "verify_spatial_claim: INCONSISTENT claimed_m=%.1f computed_m=%.1f "
                "delta=%.1f claim=%.120s",
                claimed_m,
                computed_distance_m,
                abs(computed_distance_m - claimed_m),
                claim_text,
            )

        return ClaimSpatialVerification(
            claim_text=claim_text,
            status=status,  # type: ignore[arg-type]
            distance_m=computed_distance_m,
            focus_summary=focus_summary,
        )

    except Exception:
        logger.warning(
            "verify_spatial_claim: unexpected error (returning indeterminate)",
            exc_info=True,
        )
        return ClaimSpatialVerification(
            claim_text=claim_text,
            status="indeterminate",
            distance_m=None,
            focus_summary=focus_summary,
        )


async def _fetch_collar_distance(
    pg_pool: Any,
    collar_id: str,
    focus_lon: float,
    focus_lat: float,
) -> float | None:
    """Fetch the ST_Distance (metres) from a silver collar to the focus point.

    Returns None if the collar is not found or the query fails.
    """
    try:
        async with asyncio.timeout(_POSTGIS_TIMEOUT_S):
            async with pg_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT ST_Distance(
                        geom::geography,
                        ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography
                    ) AS distance_m
                    FROM silver.collars
                    WHERE collar_id = $1::uuid
                      AND geom IS NOT NULL
                    LIMIT 1
                    """,
                    collar_id,
                    focus_lon,
                    focus_lat,
                )
        if row is None:
            return None
        raw = row["distance_m"]
        return float(raw) if raw is not None else None
    except Exception:
        logger.debug("_fetch_collar_distance: query failed (non-fatal)", exc_info=True)
        return None


def _focus_centroid(
    focus: dict[str, Any],
) -> tuple[float | None, float | None]:
    """Extract (lon, lat) centroid from a ConversationState.spatial_focus dict.

    Supports two shapes:
      bbox   — {'minx', 'miny', 'maxx', 'maxy', 'crs'}
      point  — {'lat', 'lon', 'radius_m'}

    Returns (None, None) on any parse error.
    """
    try:
        if "lat" in focus and "lon" in focus:
            return float(focus["lon"]), float(focus["lat"])
        if all(k in focus for k in ("minx", "miny", "maxx", "maxy")):
            lon = (float(focus["minx"]) + float(focus["maxx"])) / 2.0
            lat = (float(focus["miny"]) + float(focus["maxy"])) / 2.0
            return lon, lat
    except Exception:
        logger.debug("_focus_centroid: parse error", exc_info=True)
    return None, None


def _summarise_spatial_focus(focus: dict[str, Any] | None) -> str:
    """Return a short human-readable summary of the spatial focus dict."""
    if focus is None:
        return "no spatial focus"
    try:
        if "lat" in focus and "lon" in focus:
            return f"point({focus['lat']:.4f},{focus['lon']:.4f}) r={focus.get('radius_m', '?')}m"
        if all(k in focus for k in ("minx", "miny", "maxx", "maxy")):
            return (
                f"bbox({focus['minx']:.4f},{focus['miny']:.4f},"
                f"{focus['maxx']:.4f},{focus['maxy']:.4f})"
            )
    except Exception:
        pass
    return str(focus)[:80]


# ---------------------------------------------------------------------------
# Temporal verification helper
# ---------------------------------------------------------------------------

_TEMPORAL_TOLERANCE_YEARS: int = 1
"""Allow ±1 year tolerance for temporal claim comparison."""


async def verify_temporal_claim(
    claim: Any,  # ClaimVerification from decomposition
    focus: tuple | None,  # ConversationState.temporal_focus = tuple[date, date] | None
    pg_pool: Any,  # asyncpg Pool
) -> ClaimTemporalVerification:
    """Verify a temporal claim against the source document's date fields.

    Checks the cited passage's published_at / effective_date / report_date from
    the §04e schema against the claim's stated timeframe.

    Status semantics:
      consistent    — the document date falls within the claim's stated timeframe
      inconsistent  — the document date contradicts the claim's timeframe
      indeterminate — no date available in source OR no temporal signal found

    Never raises.  Any exception → indeterminate + log.

    Args:
        claim:    ClaimVerification instance.
        focus:    ConversationState.temporal_focus (date, date) tuple, or None.
        pg_pool:  asyncpg Pool for date lookup.

    Returns:
        ClaimTemporalVerification with status, document_date, and focus_summary.
    """
    from app.models.decomposition import ClaimTemporalVerification  # noqa: PLC0415

    claim_text: str = getattr(claim, "claim_text", "") or ""
    focus_summary: str = _summarise_temporal_focus(focus)

    try:
        signals = extract_temporal_signals(claim_text)
        if not signals:
            return ClaimTemporalVerification(
                claim_text=claim_text,
                status="indeterminate",
                document_date=None,
                focus_summary=focus_summary,
            )

        if pg_pool is None:
            return ClaimTemporalVerification(
                claim_text=claim_text,
                status="indeterminate",
                document_date=None,
                focus_summary=focus_summary,
            )

        # Phase 6.A — read source_chunk_id from the real ClaimVerification field.
        source_chunk_id: str = getattr(claim, "source_chunk_id", None) or ""
        document_date_str: str | None = None
        doc_year: int | None = None

        if source_chunk_id:
            document_date_str = await _fetch_document_date(pg_pool, source_chunk_id)
            if document_date_str:
                try:
                    doc_year = int(document_date_str[:4])
                except (ValueError, IndexError):
                    doc_year = None

        if doc_year is None:
            # No date resolved — indeterminate.
            return ClaimTemporalVerification(
                claim_text=claim_text,
                status="indeterminate",
                document_date=document_date_str,
                focus_summary=focus_summary,
            )

        # Evaluate the first temporal signal against the document year.
        signal = signals[0]
        status = _evaluate_temporal_signal(signal, doc_year)

        if status == "inconsistent":
            logger.info(
                "verify_temporal_claim: INCONSISTENT signal_type=%s matched=%s "
                "doc_year=%d claim=%.120s",
                signal.signal_type,
                signal.matched_text,
                doc_year,
                claim_text,
            )

        return ClaimTemporalVerification(
            claim_text=claim_text,
            status=status,  # type: ignore[arg-type]
            document_date=document_date_str,
            focus_summary=focus_summary,
        )

    except Exception:
        logger.warning(
            "verify_temporal_claim: unexpected error (returning indeterminate)",
            exc_info=True,
        )
        return ClaimTemporalVerification(
            claim_text=claim_text,
            status="indeterminate",
            document_date=None,
            focus_summary=focus_summary,
        )


def _evaluate_temporal_signal(signal: TemporalSignal, doc_year: int) -> str:
    """Compare a TemporalSignal against a known document year.

    Returns 'consistent', 'inconsistent', or 'indeterminate'.
    """
    tol = _TEMPORAL_TOLERANCE_YEARS
    if signal.signal_type == "year":
        if signal.year_start is not None:
            if abs(doc_year - signal.year_start) <= tol:
                return "consistent"
            return "inconsistent"
    elif signal.signal_type == "date_range":
        if signal.year_start is not None and signal.year_end is not None:
            lo = signal.year_start - tol
            hi = signal.year_end + tol
            if lo <= doc_year <= hi:
                return "consistent"
            return "inconsistent"
    elif signal.signal_type == "relative_prior":
        if signal.year_start is not None:
            if doc_year < signal.year_start + tol:
                return "consistent"
            return "inconsistent"
    elif signal.signal_type == "relative_after":
        if signal.year_start is not None:
            if doc_year > signal.year_start - tol:
                return "consistent"
            return "inconsistent"
    return "indeterminate"


async def _fetch_document_date(
    pg_pool: Any,
    source_chunk_id: str,
) -> str | None:
    """Look up the earliest non-null date for the source document.

    Tries published_at, effective_date, and report_date from silver.reports,
    falling back to passage-level metadata if the source_chunk_id is a Qdrant
    vector ID (UUID format) via silver.document_passages → silver.reports.

    Returns an ISO-8601 date string (YYYY-MM-DD) or None if unresolvable.
    """
    if not source_chunk_id:
        return None
    try:
        async with asyncio.timeout(_POSTGIS_TIMEOUT_S):
            async with pg_pool.acquire() as conn:
                # Try silver.reports directly if source_chunk_id looks like a report key.
                # silver:reports:{pk} format from factual_lookup.
                if source_chunk_id.startswith("silver:reports:"):
                    report_pk = source_chunk_id.split(":")[-1]
                    row = await conn.fetchrow(
                        """
                        SELECT COALESCE(
                            effective_date::text,
                            report_date::text,
                            published_at::text
                        ) AS doc_date
                        FROM silver.reports
                        WHERE report_id = $1::uuid
                        LIMIT 1
                        """,
                        report_pk,
                    )
                    if row and row["doc_date"]:
                        return str(row["doc_date"])[:10]

                # Try via passage → report join for Qdrant vector IDs (UUID form).
                try:
                    import uuid as _uuid_mod  # noqa: PLC0415
                    _uuid_mod.UUID(source_chunk_id)  # validates UUID format
                    row = await conn.fetchrow(
                        """
                        SELECT COALESCE(
                            r.effective_date::text,
                            r.report_date::text,
                            r.published_at::text
                        ) AS doc_date
                        FROM silver.document_passages dp
                        JOIN silver.reports r ON r.report_id = dp.report_id
                        WHERE dp.passage_id = $1::uuid
                        LIMIT 1
                        """,
                        source_chunk_id,
                    )
                    if row and row["doc_date"]:
                        return str(row["doc_date"])[:10]
                except (ValueError, AttributeError):
                    pass  # not a UUID — not a passage-backed chunk

        return None
    except Exception:
        logger.debug("_fetch_document_date: query failed (non-fatal)", exc_info=True)
        return None


def _summarise_temporal_focus(focus: tuple | None) -> str:
    """Return a short human-readable summary of the temporal focus tuple."""
    if focus is None:
        return "no temporal focus"
    try:
        return f"[{focus[0]}, {focus[1]}]"
    except Exception:
        return str(focus)[:40]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "SpatialSignal",
    "TemporalSignal",
    "extract_spatial_signals",
    "extract_temporal_signals",
    "verify_spatial_claim",
    "verify_temporal_claim",
]
