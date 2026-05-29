"""Phase 11 Step 3 — central prompt-version registry.

Each entry is `prompt_name → (module_path, version_string)`. The
pre-commit hook `system-prompt-version-bump` flags commits that
touch a registered prompt path without updating its version
string, preventing silent prompt drift that bypasses Anthropic
cache invalidation and RETRIEVAL_STRATEGY_VERSION bookkeeping.

Add a new entry when you migrate an inline prompt — see
`app/agent/prompts/__init__.py` for the procedure.
"""

from __future__ import annotations

from typing import TypedDict


class PromptEntry(TypedDict):
    """Shape of a single registry row."""

    module: str          # dotted import path, e.g. "app.agent.prompts.example_system"
    version: str         # semver-shaped string, e.g. "0.1.0"
    description: str     # one-line summary of what the prompt instructs


PROMPT_REGISTRY: dict[str, PromptEntry] = {
    "example_system": {
        "module": "app.agent.prompts.example_system",
        "version": "0.1.0",
        "description": (
            "Phase 11 Step 3 example prompt — demonstrates the "
            "canonical pattern. Not used by any agent today."
        ),
    },
    "rephrase_system": {
        "module": "app.agent.prompts.rephrase_system",
        "version": "0.1.0",
        "description": (
            "Phase 12 Step 2 migration. Query-rewriting system prompt "
            "consumed by app.agent.escalation when the keyword "
            "classifier falls through to empty results. Output "
            "contract: strict JSON of shape {\"rephrasings\": [...]}."
        ),
    },
    "classifier_system": {
        "module": "app.agent.prompts.classifier_system",
        "version": "0.1.0",
        "description": (
            "Phase 13 Step 1 migration. Query-routing classifier "
            "system prompt consumed by app.agent.llm_classifier. "
            "Output contract: strict JSON with seven boolean keys "
            "(spatial / documents / graph / assay / downhole / "
            "targeting / public_geo)."
        ),
    },
    "agent_system": {
        "module": "app.agent.prompts.agent_system",
        "version": "0.1.0",
        "description": (
            "Phase 14 Step 1 migration. The Pydantic AI agent system "
            "prompt that drives §04p agentic escalation — twelve tools "
            "(4 core retrieval + 8 PDF subsystem) plus determinism "
            "rules for coordinates, numeric claims, and citations. "
            "Consumer: app.agent.agentic_escalation._build_agent."
        ),
    },
    "structured_answer_format": {
        "module": "app.agent.prompts.structured_answer_format",
        "version": "0.1.0",
        "description": (
            "Plan §4a structured answer format. Appended to the geology "
            "system prompt when answer_mode is 'short' or 'detailed' "
            "(skipped for 'evidence_only'). Defines the 8-section "
            "structure, value-sourcing policy (incl. supersession "
            "clause from §1h), and answer-mode selector. Consumer: "
            "app.agent.response_assembler — chooses which sections to "
            "render from the LLM output based on intent + answer_mode."
        ),
    },
    # Phase 15+ migrations land below this line. Bump the version when
    # the prompt text materially changes; the pre-commit hook enforces
    # the bump on commit if both the prompt file and the registry are
    # in the same diff.
}


__all__ = ["PROMPT_REGISTRY", "PromptEntry"]
