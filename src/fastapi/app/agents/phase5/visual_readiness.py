"""Visual Readiness Agent (§5.11 / master plan §17.5).

Front-of-pipeline agent: when a user (or chat answer) requests a
visualization, this agent first checks whether the requested
visualization is possible given what's in silver/gold for the
target collar(s) or project.

Phase H4 graduation — deterministic feasibility checks against a
caller-supplied inventory dict, matching the pattern in
``drillhole_visual_qa``. The §5 router calls this with a DB-fetched
inventory before kicking off render work; tests + dry runs pass the
inventory directly.

Output contract is operator-readable (per master plan §5: "the
Visual Readiness Agent correctly explains when a visualization is
or isn't possible").
"""
from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


VizKind = Literal["strip_log", "cross_section", "stereonet"]


# Required inventory keys per viz kind. Each entry is
# (key, min_required, friendly_name).
_VIZ_REQUIREMENTS: dict[str, list[tuple[str, int, str]]] = {
    "strip_log": [
        ("interval_count",  3, "lithology intervals in gold"),
        ("has_total_depth", 1, "collar total_depth"),
    ],
    "cross_section": [
        ("collar_count",        2, "collars on the section line"),
        ("section_line_present", 1, "silver.section_lines row"),
        ("interval_count",      6, "lithology intervals across all collars"),
    ],
    "stereonet": [
        ("structure_count", 3, "silver.structure_measurements rows"),
    ],
}


@georag_agent(
    name="Visual Readiness Agent",
    risk_tier="R1",  # Read-only; advisory output; no mutation
    version="1.0.0",  # graduated Phase H4
)
async def visual_readiness(
    ctx: AgentContext,
    *,
    viz_kind: VizKind,
    collar_id: UUID | str | None = None,
    project_id: UUID | str | None = None,
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check whether a visualization is feasible for the given target.

    Args:
        viz_kind: "strip_log" | "cross_section" | "stereonet"
        collar_id: required for strip_log + stereonet (single-collar)
        project_id: required for cross_section (multi-collar)
        inventory: caller-supplied data inventory dict. Keys depend
            on viz_kind (see _VIZ_REQUIREMENTS). When None, the agent
            returns a not-ready response with an explanation.

    Returns:
        Operator-readable feasibility envelope.
    """
    if viz_kind not in _VIZ_REQUIREMENTS:
        return {
            "ready":     False,
            "supported": [],
            "message":   f"unknown viz_kind={viz_kind!r}",
            "missing":   ["valid viz_kind"],
            "warnings":  [],
        }

    if collar_id is None and viz_kind in ("strip_log", "stereonet"):
        return {
            "ready":     False,
            "supported": [],
            "message":   f"{viz_kind} requires collar_id",
            "missing":   ["collar_id"],
            "warnings":  [],
        }
    if project_id is None and viz_kind == "cross_section":
        return {
            "ready":     False,
            "supported": [],
            "message":   "cross_section requires project_id",
            "missing":   ["project_id"],
            "warnings":  [],
        }

    if inventory is None:
        return {
            "ready":     False,
            "supported": [],
            "message":   (
                f"no inventory supplied — agent needs caller to "
                f"pre-fetch counts for {viz_kind}"
            ),
            "missing":   ["inventory"],
            "warnings":  [],
        }

    requirements = _VIZ_REQUIREMENTS[viz_kind]
    missing: list[str] = []
    warnings: list[str] = []
    for key, min_required, friendly in requirements:
        actual = int(inventory.get(key, 0) or 0)
        if actual < min_required:
            if actual == 0:
                missing.append(f"{friendly} (0 rows; need ≥{min_required})")
            else:
                warnings.append(
                    f"{friendly} sparse: {actual} rows (recommended ≥{min_required})"
                )

    ready = len(missing) == 0
    message = None
    if not ready:
        target = (
            f"collar_id={collar_id}" if collar_id else f"project_id={project_id}"
        )
        message = (
            f"Not enough data to draw {viz_kind} for {target}: "
            + "; ".join(missing) + "."
        )
    elif warnings:
        message = f"{viz_kind} can be drawn, but: " + "; ".join(warnings)

    supported = [viz_kind] if ready else []

    summary = (
        f"viz_kind={viz_kind} ready={ready} missing={len(missing)} "
        f"warnings={len(warnings)}"
    )
    logger.info("visual_readiness: %s", summary)

    return {
        "ready":     ready,
        "supported": supported,
        "message":   message,
        "missing":   missing,
        "warnings":  warnings,
        "summary":   summary,
    }


__all__ = ["visual_readiness", "VizKind"]
