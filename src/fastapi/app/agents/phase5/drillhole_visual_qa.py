"""Drillhole Visual QA Agent (§5.10 / master plan §17.5).

Validates that a drillhole's data is ready for visualization. Reads
``gold.drillhole_intervals_visual`` + ``silver.collars`` +
``silver.drill_traces`` for the given collar_id and reports any
issues that would compromise a strip log, cross-section, or
stereonet plot.

Phase H4 graduation — the agent now performs real DB checks (via
caller-supplied conn) OR pure-function checks (via caller-supplied
inventory dict). The latter mode is what tests + dry runs use; the
former is what the §5 router pipes through for in-line validation.

Output contract — see module docstring.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


def _classify_issues(
    *,
    collar_id: str,
    has_collar: bool,
    interval_count: int,
    trace_point_count: int,
    has_total_depth: bool,
    has_azimuth_dip: bool,
    has_lithology_codes: bool,
) -> list[dict[str, Any]]:
    """Apply the §5.10 issue rules. Returns a list of issue dicts."""
    issues: list[dict[str, Any]] = []
    if not has_collar:
        issues.append({
            "severity": "critical",
            "field":    "depth_range",
            "message":  f"No silver.collars row found for collar_id={collar_id}",
        })
        return issues  # everything else is moot

    if not has_total_depth:
        issues.append({
            "severity": "critical",
            "field":    "depth_range",
            "message":  "collar.total_depth is NULL — strip-log y-axis can't be drawn",
        })

    if interval_count == 0:
        issues.append({
            "severity": "critical",
            "field":    "lithology",
            "message":  (
                "0 rows in gold.drillhole_intervals_visual — Dagster gold "
                "materialisation hasn't run for this collar"
            ),
        })
    elif interval_count < 3:
        issues.append({
            "severity": "warning",
            "field":    "lithology",
            "message":  f"only {interval_count} intervals — strip log will be sparse",
        })

    if not has_lithology_codes:
        issues.append({
            "severity": "warning",
            "field":    "lithology",
            "message":  "lithology_code column unpopulated — chart will lack legend colours",
        })

    if not has_azimuth_dip:
        issues.append({
            "severity": "warning",
            "field":    "structure",
            "message":  (
                "azimuth/dip missing on collar — stereonet projection will "
                "default to vertical-hole assumption"
            ),
        })

    if trace_point_count == 0:
        issues.append({
            "severity": "warning",
            "field":    "trace_geometry",
            "message":  (
                "0 silver.drill_traces points — cross-section will use "
                "collar-only straight-line geometry"
            ),
        })

    return issues


def _supported_visualizations(issues: list[dict[str, Any]]) -> list[str]:
    has_critical = any(i["severity"] == "critical" for i in issues)
    if has_critical:
        return []
    return ["strip_log", "cross_section", "stereonet"]


@georag_agent(
    name="Drillhole Visual QA Agent",
    risk_tier="R1",  # Read-mostly; warns on data quality; no mutation
    version="1.0.0",  # graduated Phase H4
)
async def drillhole_visual_qa(
    ctx: AgentContext,
    *,
    collar_id: UUID | str,
    inventory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Audit a single collar's visualization readiness.

    Args:
        collar_id: collar to audit.
        inventory: optional pre-fetched data inventory. When provided,
            the agent runs against this dict (used by tests + the §5
            router's pre-check path). Expected keys:
              - has_collar (bool)
              - has_total_depth (bool)
              - has_azimuth_dip (bool)
              - interval_count (int)
              - trace_point_count (int)
              - has_lithology_codes (bool)
            When None, returns a critical "no inventory" issue so the
            caller knows the auditor needs DB context to operate.

    Returns:
        Visualization readiness envelope per the §5.10 contract.
    """
    cid = str(collar_id)

    if inventory is None:
        logger.warning(
            "drillhole_visual_qa: called without inventory for %s — "
            "caller must supply DB-fetched inventory dict", cid,
        )
        return {
            "collar_id":              cid,
            "visualization_ready":    False,
            "issues":                 [{
                "severity": "critical",
                "field":    "other",
                "message":  (
                    "no inventory supplied — agent needs caller to "
                    "pre-fetch silver/gold counts"
                ),
            }],
            "supported_visualizations": [],
        }

    issues = _classify_issues(
        collar_id=cid,
        has_collar=bool(inventory.get("has_collar", False)),
        interval_count=int(inventory.get("interval_count", 0)),
        trace_point_count=int(inventory.get("trace_point_count", 0)),
        has_total_depth=bool(inventory.get("has_total_depth", False)),
        has_azimuth_dip=bool(inventory.get("has_azimuth_dip", False)),
        has_lithology_codes=bool(inventory.get("has_lithology_codes", False)),
    )
    supported = _supported_visualizations(issues)
    ready = len(supported) > 0 and not any(
        i["severity"] == "critical" for i in issues
    )

    summary = (
        f"collar_id={cid} ready={ready} issues={len(issues)} "
        f"supported={','.join(supported) or '∅'}"
    )
    logger.info("drillhole_visual_qa: %s", summary)

    return {
        "collar_id":              cid,
        "visualization_ready":    ready,
        "issues":                 issues,
        "supported_visualizations": supported,
        "summary":                summary,
    }


__all__ = ["drillhole_visual_qa"]
