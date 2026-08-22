"""Every module in app/agent/prompts/ must have a live importer.

WHY THIS TEST EXISTS
    Eight modules used to sit in this package named
    orchestrator_{default,graph,narrative,numeric}_{colon,dash}.py. Each
    one's docstring declared ``Consumer: app.agent.orchestrator``. The
    pre-commit hook `system-prompt-version-bump` watched the directory.
    Everything about them said "this is the live RAG system prompt".

    Nothing imported them. The orchestrator built its prompts from inline
    string literals in orchestrator/__init__.py, and had done since the
    extraction was abandoned half-finished — the two shared preambles were
    migrated and imported, the eight bodies were not.

    The damage is not that they wasted 633 lines. It is that they had
    DRIFTED, and in the direction that matters: the live inline prompts
    had been refined afterwards, so the orphans held older, blunter
    instructions. The refusal rule is the clearest case —

        orphan module:  If the context is empty say "I don't have data on
                        that in this project."
        live inline:    If retrieval returned no passages, or the passages
                        are genuinely ...

    — and the numeric variant's orphan had lost a whole clause about
    falling back to narrative passages when the summaries block is absent.
    So the tempting fix, "finish the migration by importing them", would
    have silently REVERTED real work on the answer path while looking like
    a tidy-up. Measured before deleting: 1 of 10 modules still matched
    what the orchestrator actually used.

    That is the failure this test prevents. Not dead code — a dead file
    that is convincingly dressed as a live one, so the next person to
    improve a prompt edits the copy that does nothing.

IF THIS TEST FAILS
    You added a module here and nothing imports it yet. Either wire it up
    in the same change, or leave it in _drafts/ until you do. Do not add
    it to the exemption list below to make the test pass.
"""

from __future__ import annotations

import re
from pathlib import Path

FASTAPI_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = FASTAPI_ROOT / "app" / "agent" / "prompts"
APP_DIR = FASTAPI_ROOT / "app"

# Modules that are legitimately not imported by name.
EXEMPT = {
    "__init__",          # the package itself
    "_version_registry",  # imported via the package __init__
}


def _prompt_modules() -> list[str]:
    return sorted(
        path.stem
        for path in PROMPTS_DIR.glob("*.py")
        if path.stem not in EXEMPT
    )


def _importers(module: str) -> list[str]:
    """Files under app/ that import `module`, excluding the module itself."""
    escaped = re.escape(module)
    pattern = re.compile(
        rf"(?:from\s+app\.agent\.prompts\.{escaped}\s+import"
        rf"|from\s+app\.agent\.prompts\s+import\s+[^\n]*\b{escaped}\b"
        rf"|import\s+app\.agent\.prompts\.{escaped}\b)"
    )
    found = []
    for path in APP_DIR.rglob("*.py"):
        if path.stem == module and path.parent == PROMPTS_DIR:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover - unreadable file
            continue
        if pattern.search(text):
            found.append(str(path.relative_to(FASTAPI_ROOT)))
    return found


def test_prompts_package_is_not_empty() -> None:
    """Guards the guard: a glob that matches nothing would pass silently."""
    modules = _prompt_modules()
    assert len(modules) >= 5, (
        f"expected several prompt modules, found {len(modules)}: {modules}"
    )


def test_every_prompt_module_has_a_live_importer() -> None:
    orphans = {
        module: _importers(module)
        for module in _prompt_modules()
    }
    dead = sorted(module for module, users in orphans.items() if not users)

    assert not dead, (
        "These prompt modules are imported by nothing under app/:\n"
        + "\n".join(f"  - app/agent/prompts/{m}.py" for m in dead)
        + "\n\nA prompt file that nothing imports is worse than no file: it "
          "reads as the live prompt, so the next person to tighten an "
          "instruction edits it, bumps PROMPT_VERSION, watches the hook and "
          "the tests pass, and changes nothing about what the model is told. "
          "Wire it up or move it to app/agent/prompts/_drafts/."
    )


def test_shared_preambles_are_the_ones_the_orchestrator_uses() -> None:
    """The two survivors really are live, not just importable.

    Deleting the eight orphans is only correct if these two are genuinely
    the wired-up ones, so assert the orchestrator's constants ARE these
    module's strings rather than copies that happen to match today.
    """
    from app.agent import orchestrator
    from app.agent.prompts import orchestrator_shared_preamble_colon as colon
    from app.agent.prompts import orchestrator_shared_preamble_dash as dash

    assert orchestrator._SYSTEM_PROMPT_SHARED_PREAMBLE_COLON is colon.SYSTEM_PROMPT
    assert orchestrator._SYSTEM_PROMPT_SHARED_PREAMBLE is dash.SYSTEM_PROMPT
