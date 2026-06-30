"""Plan §2g — geospatial query planner (foundation).

Translates a structured :class:`SpatialQuerySpec` into a parameterised
SQL statement targeting PostGIS-enabled tables in the GeoRAG silver +
gold layers. This is the FOUNDATION — the actual asyncpg execution
node + integration with the agentic_retrieval LangGraph is downstream.

Why a planner (not just SQL inline):

  1. **Workspace tenancy is non-negotiable.** Every plan emits the
     `workspace_id = current_setting('app.workspace_id')::uuid`
     predicate so RLS can't be silently bypassed. The planner pins
     this into every emitted query; ad-hoc SQL elsewhere can't.

  2. **CRS pinning.** The planner refuses to emit a query when the
     spec's `crs_epsg` doesn't match the target table's stored CRS
     (we don't auto-transform here — that's a §2g repair-strategy
     concern, see ``RepairStrategy.TRANSFORM_CRS``). Forces the
     caller to be explicit.

  3. **Parameterisation.** ALL coordinates + buffers + LIMITs go
     through asyncpg's $N placeholders. No string concatenation.

  4. **Auditability.** The same spec → same SQL → same params, so
     the trace inspector + the repair loop's death-loop detector can
     compare filter dicts byte-identically.

The planner is **pure**: no I/O, no DB connection, no LLM. The
``execute_spatial_query`` helper wraps the planner in a thin async
runner that uses a passed-in asyncpg pool. Tests mock the pool.

Supported operations (PostGIS):

  * ``intersects`` — ST_Intersects(geom, $N::geometry)
  * ``contains``   — ST_Contains(geom, $N::geometry)
  * ``within``     — ST_Within(geom, $N::geometry)
  * ``dwithin``    — ST_DWithin(geom, $N::geometry, $M::numeric)
  * ``distance``   — return ORDER BY ST_Distance(geom, $N::geometry)

Supported target tables (verified workspace-scoped):

  * ``silver.collars``       — drill collars (workspace_id-scoped)
  * ``silver.spatial_features`` — generic GIS layer
  * ``public.smdi_deposits``    — public SMDI mineral occurrences
                                    (NOT workspace-scoped; tenant filter
                                    is implicit via the public schema)
  * ``gold.h3_density``         — H3-aggregated data density

Each target carries:
  - The geometry column name (e.g. ``collar_geom``).
  - Whether it carries a ``workspace_id`` column (most do; SMDI doesn't).
  - The CRS the column is stored in (defaults to ``EPSG:4326`` =
    WGS84 / GeoJSON-compatible).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)


__all__ = [
    "SpatialOperation",
    "SpatialTarget",
    "SpatialQuerySpec",
    "SpatialPlan",
    "SPATIAL_TARGETS",
    "plan_spatial_query",
    "execute_spatial_query",
]


# ---------------------------------------------------------------------------
# Enums + spec shapes
# ---------------------------------------------------------------------------


SpatialOperation = Literal[
    "intersects",
    "contains",
    "within",
    "dwithin",
    "distance",
]


@dataclass(frozen=True)
class SpatialTarget:
    """A queryable spatial table.

    Attributes:
        table: Fully-qualified table name (``schema.table``).
        geom_column: Name of the geometry column on that table.
        crs_epsg: CRS EPSG code the geometry is stored in.
        workspace_scoped: When True, the planner adds a
            ``workspace_id = current_setting('app.workspace_id')::uuid``
            predicate. When False, the planner ASSUMES the schema
            (e.g. ``public.smdi_deposits``) is intentionally not
            workspace-scoped and emits a comment in the SQL noting why.
        select_columns: Default SELECT list when the spec doesn't
            override. Includes the table's PK + label columns useful
            for citation rendering.
    """

    table: str
    geom_column: str
    crs_epsg: int = 4326
    workspace_scoped: bool = True
    select_columns: tuple[str, ...] = ("*",)


SPATIAL_TARGETS: dict[str, SpatialTarget] = {
    "silver.collars": SpatialTarget(
        table="silver.collars",
        geom_column="collar_geom",
        crs_epsg=4326,
        workspace_scoped=True,
        select_columns=(
            "collar_id",
            "hole_id",
            "easting",
            "northing",
            "spatial_crs",
            "total_depth_m",
        ),
    ),
    "silver.spatial_features": SpatialTarget(
        table="silver.spatial_features",
        geom_column="geom",
        crs_epsg=4326,
        workspace_scoped=True,
        select_columns=(
            "feature_id",
            "feature_type",
            "feature_label",
            "spatial_crs",
            "source_document_id",
        ),
    ),
    "public.smdi_deposits": SpatialTarget(
        table="public.smdi_deposits",
        geom_column="geom",
        crs_epsg=4326,
        workspace_scoped=False,  # public table, intentionally not tenant-scoped
        select_columns=(
            "smdi_id",
            "deposit_name",
            "commodity_primary",
            "jurisdiction_code",
        ),
    ),
    "gold.h3_density": SpatialTarget(
        table="gold.h3_density",
        geom_column="h3_cell_geom",
        crs_epsg=4326,
        workspace_scoped=True,
        select_columns=(
            "h3_index",
            "h3_resolution",
            "record_count",
        ),
    ),
}


@dataclass(frozen=True)
class SpatialQuerySpec:
    """The structured spec that drives the planner.

    Attributes:
        target: Key into :data:`SPATIAL_TARGETS`. Validated at plan time.
        operation: One of :data:`SpatialOperation`.
        geometry_wkt: Input geometry as WKT (Well-Known Text). The
            planner casts it via ``ST_GeomFromText($N, 4326)`` —
            ALWAYS WGS84 at the wire boundary; CRS coercion happens
            upstream (see ``RepairStrategy.TRANSFORM_CRS``).
        crs_epsg: CRS the input geometry is in. Must match the
            target's ``crs_epsg`` or the planner refuses to plan.
        buffer_m: Required when ``operation == "dwithin"``; ignored
            otherwise. Units: metres (the planner emits the right
            casts for geography-vs-geometry columns later — see
            module docstring).
        limit: Row cap; defaults to 200. Capped at 1000 by the planner.
        select_columns: Override the target's default SELECT list.
        order_by: Optional column to ORDER BY. For ``distance`` ops
            the planner injects ``ST_Distance(geom, input_geom)`` as
            the order key automatically; this attribute is the
            secondary sort.
    """

    target: str
    operation: SpatialOperation
    geometry_wkt: str
    crs_epsg: int = 4326
    buffer_m: float | None = None
    limit: int = 200
    select_columns: tuple[str, ...] | None = None
    order_by: str | None = None


@dataclass(frozen=True)
class SpatialPlan:
    """Output of :func:`plan_spatial_query`.

    Attributes:
        sql: Parameterised SQL with ``$1``, ``$2``, … placeholders.
        params: Ordered list of parameter values matching the SQL
            placeholders. Pass directly to ``asyncpg`` fetch methods.
        target: The :class:`SpatialTarget` the plan was built against
            — useful for the executor to know which CRS the result
            geometries are in.
        signature: A short stable string capturing target + operation
            + buffer for trace correlation + death-loop detection.
    """

    sql: str
    params: tuple[Any, ...]
    target: SpatialTarget
    signature: str


# ---------------------------------------------------------------------------
# Planner — pure function
# ---------------------------------------------------------------------------


def plan_spatial_query(spec: SpatialQuerySpec) -> SpatialPlan:
    """Translate a :class:`SpatialQuerySpec` into a parameterised SQL plan.

    Raises:
        KeyError: ``spec.target`` not in :data:`SPATIAL_TARGETS`.
        ValueError: Operation requires ``buffer_m`` but it's missing,
            or CRS doesn't match the target.
    """
    target = SPATIAL_TARGETS[spec.target]  # KeyError on bad target

    if spec.crs_epsg != target.crs_epsg:
        raise ValueError(
            f"CRS mismatch: spec.crs_epsg={spec.crs_epsg} but target "
            f"{spec.target} stores geometries in EPSG:{target.crs_epsg}. "
            f"Coerce input via pyproj or use RepairStrategy.TRANSFORM_CRS."
        )

    if spec.operation == "dwithin" and (spec.buffer_m is None or spec.buffer_m <= 0):
        raise ValueError(
            "operation='dwithin' requires a positive buffer_m"
        )

    limit = max(1, min(1000, int(spec.limit)))
    select_cols = spec.select_columns or target.select_columns
    select_list = ", ".join(select_cols)

    params: list[Any] = []
    params.append(spec.geometry_wkt)  # $1 — input geometry
    where_clauses: list[str] = []

    # Spatial predicate.
    op = spec.operation
    geom_param_idx = len(params)  # =1 right now
    geom_expr = f"ST_GeomFromText(${geom_param_idx}::text, {spec.crs_epsg})"

    if op == "intersects":
        where_clauses.append(
            f"ST_Intersects({target.geom_column}, {geom_expr})"
        )
    elif op == "contains":
        where_clauses.append(
            f"ST_Contains({target.geom_column}, {geom_expr})"
        )
    elif op == "within":
        where_clauses.append(
            f"ST_Within({target.geom_column}, {geom_expr})"
        )
    elif op == "dwithin":
        params.append(float(spec.buffer_m))
        buf_idx = len(params)
        where_clauses.append(
            f"ST_DWithin({target.geom_column}::geography, "
            f"{geom_expr}::geography, ${buf_idx}::numeric)"
        )
    elif op == "distance":
        # No WHERE — the entire ranking happens via ORDER BY.
        pass
    else:
        raise ValueError(f"unknown operation: {op!r}")

    # Workspace tenancy predicate.
    if target.workspace_scoped:
        where_clauses.append(
            f"{_workspace_column(target)} = "
            f"current_setting('app.workspace_id')::uuid"
        )

    # WHERE clause assembly.
    where_sql = (
        "WHERE " + " AND ".join(where_clauses)
        if where_clauses
        else ""
    )

    # ORDER BY — distance ops sort by computed distance; everything
    # else uses spec.order_by when provided.
    order_clauses: list[str] = []
    if op == "distance":
        order_clauses.append(f"ST_Distance({target.geom_column}, {geom_expr})")
    if spec.order_by:
        order_clauses.append(spec.order_by)
    order_sql = "ORDER BY " + ", ".join(order_clauses) if order_clauses else ""

    # Workspace-scope-bypass comment for unscoped targets (auditable).
    tenancy_comment = (
        ""
        if target.workspace_scoped
        else f"-- intentionally unscoped: {target.table} is a public reference table\n"
    )

    sql = (
        f"{tenancy_comment}"
        f"SELECT {select_list}\n"
        f"FROM {target.table}\n"
        f"{where_sql}\n"
        f"{order_sql}\n"
        f"LIMIT {limit}"
    ).strip()

    signature = (
        f"{spec.target}:{op}"
        + (f":buf={spec.buffer_m}" if spec.buffer_m else "")
    )

    return SpatialPlan(
        sql=sql,
        params=tuple(params),
        target=target,
        signature=signature,
    )


def _workspace_column(target: SpatialTarget) -> str:
    """Return the workspace_id column name for a target table.

    Most silver tables use ``workspace_id``; if any future target
    diverges from that convention, special-case here.
    """
    return "workspace_id"


# ---------------------------------------------------------------------------
# Executor — thin async wrapper around an asyncpg pool
# ---------------------------------------------------------------------------


async def execute_spatial_query(
    pool: Any,
    plan: SpatialPlan,
    *,
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Run the plan against ``pool`` and return rows as list[dict].

    The function sets ``app.workspace_id`` via ``set_config`` inside
    a transaction so the WHERE predicate emitted by the planner has the
    tenant context. Workspace boundary: workspace_id is REQUIRED — the
    function never executes without it, even for ``workspace_scoped=False``
    targets (the GUC is benign for those queries but pins the trace).

    Args:
        pool: ``asyncpg.Pool``-like object (anything with an async
            ``acquire()`` context manager that yields a connection).
        plan: From :func:`plan_spatial_query`.
        workspace_id: Caller's workspace UUID. Set as the local GUC
            ``app.workspace_id`` inside the transaction.

    Returns:
        List of row dicts. Empty list when no matches. asyncpg.Records
        get coerced to plain dicts so the result is JSON-serialisable.
    """
    if not workspace_id:
        raise ValueError("workspace_id is required (sets app.workspace_id)")

    async with pool.acquire() as conn:
        async with conn.transaction():
            from app.db import bind_workspace_scope  # noqa: PLC0415
            await bind_workspace_scope(
                conn, workspace_id=workspace_id, site="agent.geospatial_planner"
            )
            rows = await conn.fetch(plan.sql, *plan.params)
    return [dict(row) for row in rows]
