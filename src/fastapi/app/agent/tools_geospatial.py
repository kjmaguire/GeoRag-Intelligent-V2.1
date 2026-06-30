"""Plan §2g — geospatial query tool (wired).

Thin async tool function that:

  1. Builds a :class:`SpatialQuerySpec` from caller-supplied parameters
     (or extracts one heuristically from query keywords as a fallback)
  2. Plans the parameterised SQL via
     :func:`app.agent.geospatial_planner.plan_spatial_query`
  3. Executes against the asyncpg pool via
     :func:`app.agent.geospatial_planner.execute_spatial_query` with
     ``app.workspace_id`` GUC set
  4. Wraps the results in a typed :class:`SpatialGeometryResult` so
     ``extract_spatial_evidence`` (the §3a converter) can pick them up

Two call shapes:

  * **Structured** — caller passes ``target``, ``operation``,
    ``geometry_wkt``, etc. directly. This is the path used by an
    eventual classifier/orchestrator-side spec builder.
  * **Keyword extraction** — when ``geometry_wkt`` is None and a
    ``query_text`` is supplied, the tool runs
    :func:`extract_spatial_intent_keywords` to derive a best-effort
    operation + buffer + target from the query.  Geometry must
    still be supplied via the project bbox or default — this
    function does NOT invent geometries.

The keyword extractor is intentionally narrow: it only flips the
``operation`` and ``buffer_m`` fields and picks the target table from
unambiguous nouns ("collars" → silver.collars, "smdi" / "occurrence"
→ public.smdi_deposits, etc.). LLM-based intent extraction is a
future enhancement.

Pure-async + workspace-scoped — sets ``app.workspace_id`` inside
the transaction. Workspace_id is REQUIRED; raises ValueError if
missing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from app.agent.geospatial_planner import (
    SPATIAL_TARGETS,
    SpatialOperation,
    SpatialPlan,
    SpatialQuerySpec,
    execute_spatial_query,
    plan_spatial_query,
)

logger = logging.getLogger(__name__)


__all__ = [
    "SpatialGeometryResult",
    "SpatialIntentHints",
    "extract_spatial_intent_keywords",
    "query_spatial_geometry",
]


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpatialGeometryResult:
    """Output of :func:`query_spatial_geometry`.

    Attributes:
        target: The :data:`SPATIAL_TARGETS` key the query hit.
        operation: Which PostGIS predicate was applied.
        count: Number of rows returned.
        rows: Raw row dicts. Caller is responsible for mapping into
            ``SpatialEvidence`` via the converter.
        plan_signature: From the planner — useful for the trace.
        buffer_m: When operation was ``dwithin``, the buffer used.
    """

    target: str
    operation: SpatialOperation
    count: int
    rows: list[dict[str, Any]]
    plan_signature: str
    buffer_m: float | None = None


@dataclass(frozen=True)
class SpatialIntentHints:
    """Output of :func:`extract_spatial_intent_keywords`.

    Each field is None when the keyword extractor didn't detect a
    confident signal — the caller can fall back to a default.
    """

    operation: SpatialOperation | None = None
    buffer_m: float | None = None
    target: str | None = None


# ---------------------------------------------------------------------------
# Keyword extractor
# ---------------------------------------------------------------------------


_OP_KEYWORDS: tuple[tuple[re.Pattern[str], SpatialOperation], ...] = (
    # Highest specificity first — 'within X meters/kilometres' is dwithin,
    # NOT plain within. Both unit families covered.
    (
        re.compile(
            r"\bwithin\s+(\d+(?:\.\d+)?)\s*"
            r"(?:m|metres?|meters?|km|kilometres?|kilometers?)\b",
            re.IGNORECASE,
        ),
        "dwithin",
    ),
    (re.compile(r"\b(?:near|close to|nearby|nearest)\b", re.IGNORECASE), "dwithin"),
    (re.compile(r"\bdistance to\b|\bsorted by distance\b", re.IGNORECASE), "distance"),
    (re.compile(r"\bcontains?\b|\bcontaining\b|\benclose[sd]?\b", re.IGNORECASE), "contains"),
    # Plain 'within' (no buffer expression after it) = polygon containment.
    (re.compile(r"\bwithin\b(?!\s+\d)", re.IGNORECASE), "within"),
    (re.compile(r"\bintersect(?:s|ing)?\b|\boverlap(?:s|ping)?\b", re.IGNORECASE), "intersects"),
)


_BUFFER_PATTERNS: tuple[tuple[re.Pattern[str], float], ...] = (
    # Explicit metres
    (re.compile(r"\bwithin\s+(\d+(?:\.\d+)?)\s*(?:m|metres?|meters?)\b", re.IGNORECASE), 1.0),
    # Explicit kilometres → metres
    (re.compile(r"\bwithin\s+(\d+(?:\.\d+)?)\s*(?:km|kilometres?|kilometers?)\b", re.IGNORECASE), 1000.0),
)


_TARGET_KEYWORDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:collar|drillhole|borehole|drill\s+hole)s?\b", re.IGNORECASE), "silver.collars"),
    (re.compile(r"\b(?:smdi|mineral\s+occurrence|deposit\s+occurrence)s?\b", re.IGNORECASE), "public.smdi_deposits"),
    (re.compile(r"\b(?:h3|density\s+grid|data\s+density)\b", re.IGNORECASE), "gold.h3_density"),
    (re.compile(r"\b(?:spatial\s+feature|gis\s+layer|polygon|outline)s?\b", re.IGNORECASE), "silver.spatial_features"),
)


def extract_spatial_intent_keywords(query_text: str) -> SpatialIntentHints:
    """Best-effort keyword extraction. None fields = no confident signal.

    Order:
      1. Buffer expressions (``within 500 m``, ``within 2 km``) → buffer_m
      2. Operation keywords (within / near / contains / intersects /
         distance to)
      3. Target keywords (collars / smdi / spatial feature / h3)

    Returns a :class:`SpatialIntentHints` where any combination of
    fields may be None.
    """
    if not query_text:
        return SpatialIntentHints()

    operation: SpatialOperation | None = None
    buffer_m: float | None = None
    target: str | None = None

    # Buffer first — extracts the number from 'within X m' phrases.
    for pattern, factor in _BUFFER_PATTERNS:
        m = pattern.search(query_text)
        if m is not None:
            try:
                buffer_m = float(m.group(1)) * factor
            except (TypeError, ValueError):
                buffer_m = None
            break

    # Operation.
    for pattern, op in _OP_KEYWORDS:
        if pattern.search(query_text):
            operation = op
            break

    # Target.
    for pattern, tgt in _TARGET_KEYWORDS:
        if pattern.search(query_text):
            target = tgt
            break

    return SpatialIntentHints(
        operation=operation, buffer_m=buffer_m, target=target,
    )


# ---------------------------------------------------------------------------
# Tool function
# ---------------------------------------------------------------------------


async def query_spatial_geometry(
    deps: Any,
    workspace_id: str,
    project_id: str | None,
    *,
    target: str | None = None,
    operation: SpatialOperation | None = None,
    geometry_wkt: str | None = None,
    crs_epsg: int = 4326,
    buffer_m: float | None = None,
    limit: int = 200,
    query_text: str | None = None,
) -> SpatialGeometryResult | None:
    """Plan + execute a PostGIS spatial query.

    Args:
        deps: Caller's AgentDeps bundle. Expects ``deps.pg_pool`` to
            be an asyncpg.Pool-like object.
        workspace_id: Tenant scope — set as the
            ``app.workspace_id`` GUC inside the transaction. RLS
            REQUIRES this; the function raises ValueError when empty.
        project_id: Currently informational only — geometry comes from
            ``geometry_wkt``. Reserved for future per-project default
            bbox lookup.
        target: One of the ``SPATIAL_TARGETS`` keys. When None AND
            ``query_text`` is supplied, the keyword extractor picks
            one; failing that, defaults to ``silver.collars``.
        operation: Spatial predicate. Same fallback chain as ``target``.
            Default ``intersects`` if neither caller nor keyword
            extractor supplied a value.
        geometry_wkt: WKT geometry to test against. REQUIRED — without
            it the tool returns ``None`` (no inventing geometries).
        crs_epsg: EPSG of the input geometry. Default 4326 (WGS84).
        buffer_m: Required for ``dwithin``; ignored otherwise.
        limit: Row cap, max 1000 enforced by the planner.
        query_text: Optional — when supplied, the keyword extractor
            fills in any of target/operation/buffer_m that the
            caller didn't pass.

    Returns:
        :class:`SpatialGeometryResult` or None (when geometry_wkt is
        absent — the orchestrator should NOT call us blind).
    """
    if not workspace_id:
        raise ValueError("workspace_id is required (sets app.workspace_id)")

    pool = getattr(deps, "pg_pool", None)
    if pool is None:
        logger.warning("query_spatial_geometry: deps.pg_pool is None")
        return None

    if geometry_wkt is None:
        # Plan §2g fallback — supply the project bounding box when the
        # caller didn't pass an explicit geometry. The supplier reads
        # silver.projects.bbox or computes ST_Envelope over the
        # project's collars. Returns None when neither resolves; the
        # tool then skips cleanly.
        try:
            from app.agent.project_geometry import get_project_bbox_wkt  # noqa: PLC0415

            geometry_wkt = await get_project_bbox_wkt(
                pool,
                workspace_id=workspace_id,
                project_id=project_id or "",
            )
        except Exception:
            logger.exception(
                "query_spatial_geometry: project-bbox supplier failed"
            )
            geometry_wkt = None

        if geometry_wkt is None:
            logger.info(
                "query_spatial_geometry: no geometry_wkt supplied and "
                "project bbox unavailable — skipping cleanly"
            )
            return None
        else:
            logger.info(
                "query_spatial_geometry: using project bbox as fallback geometry"
            )

    # Fill missing fields from keyword extraction when query_text given.
    hints = (
        extract_spatial_intent_keywords(query_text)
        if query_text else SpatialIntentHints()
    )
    final_target = target or hints.target or "silver.collars"
    final_operation: SpatialOperation = operation or hints.operation or "intersects"
    final_buffer = buffer_m if buffer_m is not None else hints.buffer_m

    if final_target not in SPATIAL_TARGETS:
        logger.warning(
            "query_spatial_geometry: unknown target %r — skipping",
            final_target,
        )
        return None

    if final_operation == "dwithin" and (final_buffer is None or final_buffer <= 0):
        # Sensible default: 500 m. Logged so the trace shows the choice.
        logger.info(
            "query_spatial_geometry: dwithin without buffer — defaulting to 500 m",
        )
        final_buffer = 500.0

    spec = SpatialQuerySpec(
        target=final_target,
        operation=final_operation,
        geometry_wkt=geometry_wkt,
        crs_epsg=crs_epsg,
        buffer_m=final_buffer,
        limit=limit,
    )

    try:
        plan: SpatialPlan = plan_spatial_query(spec)
    except (KeyError, ValueError):
        logger.exception(
            "query_spatial_geometry: plan_spatial_query rejected the spec"
        )
        return None

    try:
        rows = await execute_spatial_query(
            pool, plan, workspace_id=workspace_id,
        )
    except Exception:
        logger.exception(
            "query_spatial_geometry: executor failed for plan %s",
            plan.signature,
        )
        return None

    return SpatialGeometryResult(
        target=final_target,
        operation=final_operation,
        count=len(rows),
        rows=rows,
        plan_signature=plan.signature,
        buffer_m=final_buffer,
    )
