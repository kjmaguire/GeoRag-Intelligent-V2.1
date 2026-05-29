"""Phase 11 Step 3 — canonical home for agent prompt strings.

Today's state
-------------
Inline prompt strings live across `orchestrator.py`, `llm_classifier.py`,
and other agent files. The Phase 5 Step 3 pre-commit hook
`system-prompt-version-bump` (configured in `.pre-commit-config.yaml`)
already watches this directory pattern:

    ^(src/fastapi/app/agent/(orchestrator\\.py|prompts/.*)|src/fastapi/app/prompts/.*)$

— so the hook fires for files committed under `app/agent/prompts/*`
the moment they exist. This package provides that directory + a
`PROMPT_REGISTRY` pattern so future prompt migrations have a
canonical landing zone.

How to migrate an inline prompt
--------------------------------
1. Pick a name (e.g. `qualitative_detector_system`).
2. Create `app/agent/prompts/<name>.py` defining a module-level
   constant (typically `SYSTEM_PROMPT = "..."`).
3. Add a `PROMPT_VERSION` constant alongside (semver-shaped string).
4. Register both in `_version_registry.py`'s `PROMPT_REGISTRY` dict.
5. Update the caller to import from
   `app.agent.prompts.<name>` instead of inlining.
6. Bump `_SYSTEM_PROMPT_VERSION` in `orchestrator.py` when the
   inline original is removed — the pre-commit hook enforces this
   when the diff touches both an existing prompt path AND
   `orchestrator.py`.

This module is intentionally minimal at Phase 11 — the migration
work itself is Phase 12+ scope. Phase 11 just lays the foundation
so the pre-commit hook + version bookkeeping pattern have a real
target.
"""

from app.agent.prompts._version_registry import PROMPT_REGISTRY
from app.agent.prompts.example_system import (
    PROMPT_VERSION as EXAMPLE_PROMPT_VERSION,
)
from app.agent.prompts.example_system import (
    SYSTEM_PROMPT as EXAMPLE_SYSTEM_PROMPT,
)

__all__ = [
    "PROMPT_REGISTRY",
    "EXAMPLE_PROMPT_VERSION",
    "EXAMPLE_SYSTEM_PROMPT",
]
