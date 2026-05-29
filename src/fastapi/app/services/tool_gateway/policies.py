"""§4.3 risk-tier policies + permission checks.

Pure-function module — no DB writes, only reads + policy decisions.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

import asyncpg


class RiskTier(str, Enum):
    R0 = "R0"  # read-only — automatic
    R1 = "R1"  # internal suggestion — automatic
    R2 = "R2"  # internal write — policy check
    R3 = "R3"  # external notification — policy + audit
    R4 = "R4"  # external publish/export — approval required
    R5 = "R5"  # destructive/bulk — explicit sign-off + QP credential

    @property
    def requires_audit(self) -> bool:
        return self in {RiskTier.R3, RiskTier.R4, RiskTier.R5}

    @property
    def requires_approval(self) -> bool:
        return self in {RiskTier.R4, RiskTier.R5}

    @property
    def requires_qp_signoff(self) -> bool:
        return self == RiskTier.R5


async def resolve_effective_tier(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    tool_name: str,
) -> RiskTier | None:
    """Return the effective tier for a tool in a workspace.

    Workspaces can override a tool to a HIGHER tier (more cautious),
    never lower. Returns None if the tool isn't registered.
    """
    row = await conn.fetchrow(
        """
        SELECT t.risk_tier AS base_tier,
               p.override_tier
          FROM workspace.agent_risk_tiers t
          LEFT JOIN workspace.agent_permissions p
            ON p.tool_name = t.tool_name
           AND p.workspace_id = $1::uuid
         WHERE t.tool_name = $2
        """,
        workspace_id, tool_name,
    )
    if row is None:
        return None
    base = RiskTier(row["base_tier"])
    override = RiskTier(row["override_tier"]) if row["override_tier"] else None
    if override is None:
        return base
    # Only honour the override if it's >= base (higher tier = stricter)
    if _tier_order(override) > _tier_order(base):
        return override
    return base


async def is_workspace_allowed(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    tool_name: str,
) -> tuple[bool, str | None]:
    """Default-permissive — every workspace has every tool unless
    explicitly denied via workspace.agent_permissions.allowed=false.

    Returns (allowed, reason_if_denied).
    """
    row = await conn.fetchrow(
        """
        SELECT allowed, notes
          FROM workspace.agent_permissions
         WHERE workspace_id = $1::uuid AND tool_name = $2
        """,
        workspace_id, tool_name,
    )
    if row is None:
        return True, None  # default-permissive
    if row["allowed"]:
        return True, None
    return False, (row["notes"] or f"tool {tool_name} explicitly denied for workspace")


async def has_approval(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    tool_name: str,
    actor_user_id: int | None,
    actor_metadata: dict[str, Any] | None,
) -> tuple[bool, str | None]:
    """For R4+ tools, check the workspace's approval_requirements row.

    Returns (approved, reason_if_denied). actor_metadata may carry
    {qp_credential_verified: bool} which the gateway gets from the
    Laravel side (qp_credentials table).
    """
    row = await conn.fetchrow(
        """
        SELECT required_role, min_credentials
          FROM workspace.approval_requirements
         WHERE workspace_id = $1::uuid AND tool_name = $2
        """,
        workspace_id, tool_name,
    )
    if row is None:
        # No requirement configured — treat as approved (workspaces
        # that want strict R4+ gating must INSERT a row).
        return True, None

    raw = row["min_credentials"]
    if isinstance(raw, str):
        import json as _json
        try:
            raw = _json.loads(raw)
        except Exception:
            raw = {}
    required = dict(raw or {})
    meta = actor_metadata or {}
    for key, expected in required.items():
        if meta.get(key) != expected:
            return False, (
                f"approval requirement not met: {key}={meta.get(key)!r} "
                f"(expected {expected!r}); required_role={row['required_role']}"
            )
    return True, None


def _tier_order(t: RiskTier) -> int:
    """Strictness order — higher = stricter."""
    return {
        RiskTier.R0: 0, RiskTier.R1: 1, RiskTier.R2: 2,
        RiskTier.R3: 3, RiskTier.R4: 4, RiskTier.R5: 5,
    }[t]


__all__ = ["RiskTier", "resolve_effective_tier", "is_workspace_allowed", "has_approval"]
