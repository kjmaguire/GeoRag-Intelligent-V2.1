"""Phase 11 Step 3 — example prompt file.

Demonstrates the canonical pattern for an agent prompt module:

  - SYSTEM_PROMPT  module-level constant carrying the prompt text
  - PROMPT_VERSION semver-shaped string; bump when SYSTEM_PROMPT
                   changes materially. Mirrored in PROMPT_REGISTRY
                   so the pre-commit hook can cross-check.

This file is NOT used by any production agent today. Phase 12+
migrations should replace `example_system` with the real prompts
currently inlined in `orchestrator.py` and friends.
"""

from __future__ import annotations

PROMPT_VERSION = "0.1.0"

SYSTEM_PROMPT = (
    "You are GeoRAG, a geological intelligence assistant. Every claim "
    "MUST include a [DATA-N] citation marker linked to a tool result; "
    "uncited responses are rejected upstream. If the tool results do "
    "not contain the answer, refuse explicitly rather than inventing "
    "numbers, drill-hole IDs, or qualitative descriptors."
)


__all__ = ["PROMPT_VERSION", "SYSTEM_PROMPT"]
