"""§4 Tool Gateway — central enforcement layer for the 19 approved agent tools.

Per master plan §4: agents never call internals directly; every action
goes through `invoke_tool()` which:
  1. Validates the workspace + tool registration
  2. Checks per-workspace permissions
  3. Applies risk-tier policy (R0-R5)
  4. Honours dry-run mode (R3+ tools)
  5. Records the invocation to workspace.tool_invocations
  6. Emits audit.audit_ledger row for R3+ outcomes
  7. Returns the tool's output

The actual tool execution lives in the registered callable (passed
to `register_tool()` or referenced by name). The gateway never
embeds business logic itself — it's policy + audit only.
"""
from app.services.tool_gateway.gateway import (
    ToolGatewayContext,
    ToolGatewayResult,
    invoke_tool,
    is_tool_registered,
    list_registered_tools,
    register_tool,
)
from app.services.tool_gateway.policies import (
    RiskTier,
    is_workspace_allowed,
    resolve_effective_tier,
)

__all__ = [
    "ToolGatewayContext",
    "ToolGatewayResult",
    "invoke_tool",
    "is_tool_registered",
    "list_registered_tools",
    "register_tool",
    "RiskTier",
    "is_workspace_allowed",
    "resolve_effective_tier",
]
