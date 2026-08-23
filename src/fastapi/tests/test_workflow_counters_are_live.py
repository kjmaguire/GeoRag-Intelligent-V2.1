"""A counter that can never rise reads exactly like a real zero.

WHAT WENT WRONG
    `continuous_learning_loop` reported two numbers in its daily audit
    anchor that were structurally incapable of being anything else:

      eval_regressions_detected  initialised to 0, never assigned again on
                                 any path. It said "no quality regressions
                                 today" every day, and would have said it
                                 on the day quality collapsed.
      workspaces_evaluated       incremented once at the bottom of a loop
                                 with no `continue` and no `break`, so it
                                 was always equal to workspaces_scanned.

    Both named a step that had been deleted a month earlier. A dashboard
    renders a frozen zero identically to a measured one, so nothing about
    reading the number reveals the problem -- you have to read the code.

WHAT THIS TEST CHECKS
    Inside each scanned workflow's task body, every local initialised to a
    bare `0` must be mutated somewhere: `+=`, a re-assignment, or passed
    somewhere that could change it. A counter that is only ever read is a
    reported constant.

    This is a narrow rule on purpose. It does not try to prove a number is
    CORRECT -- only that the code is capable of producing more than one
    value. That is the property the two dead fields lacked.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parent.parent / "app" / "hatchet_workflows"

#: Every workflow module. Scanned wholesale rather than from a hand-kept
#: list because the sweep came back clean on all of them once the two dead
#: fields were removed -- so there is no burn-down to phase, and a new
#: workflow is covered the day it lands rather than the day someone
#: remembers to add it here.
SCANNED = sorted(p.name for p in WORKFLOWS.glob("*.py"))

#: Locals that are legitimately initialised to 0 and read without mutation,
#: with the reason. Empty is the healthy state.
EXEMPT: dict[tuple[str, str], str] = {}


def _zero_initialised_locals(tree: ast.AST) -> dict[str, ast.Assign]:
    """`name = 0` bindings anywhere in the module."""
    found: dict[str, ast.Assign] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value
        if not isinstance(target, ast.Name):
            continue
        if isinstance(value, ast.Constant) and value.value == 0:
            found.setdefault(target.id, node)
    return found


def _mutated_names(tree: ast.AST) -> set[str]:
    """Names that are augmented-assigned or re-assigned to non-constants."""
    mutated: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            mutated.add(node.target.id)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            value = node.value
            is_zero = isinstance(value, ast.Constant) and value.value == 0
            if not is_zero:
                mutated.add(target.id)
    return mutated


@pytest.mark.parametrize("module", SCANNED)
def test_every_zero_counter_can_actually_change(module: str) -> None:
    path = WORKFLOWS / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    zeroed = _zero_initialised_locals(tree)
    mutated = _mutated_names(tree)

    frozen = sorted(
        name for name in zeroed
        if name not in mutated and (module, name) not in EXEMPT
    )

    assert not frozen, (
        f"{module} initialises these to 0 and never changes them:\n  "
        + "\n  ".join(f"{n} (line {zeroed[n].lineno})" for n in frozen)
        + "\n\nIf one is reported anywhere — an audit payload, a log line, a "
        "workflow output — it is a constant being rendered as a measurement. "
        "Either compute it or remove it. If it is genuinely a placeholder "
        "that something else fills in, record it in EXEMPT with the reason."
    )


def test_the_scan_covers_the_whole_directory() -> None:
    """A glob that silently returns nothing would make every case above
    pass by never running."""
    assert len(SCANNED) >= 20, (
        f"only {len(SCANNED)} workflow modules found under {WORKFLOWS} — "
        "the glob is broken, not the directory empty"
    )
    assert "continuous_learning_loop.py" in SCANNED


def test_the_scan_is_not_vacuous() -> None:
    """Guards the guard: if the AST walk stops finding counters at all,
    every assertion above passes for the wrong reason."""
    tree = ast.parse(
        (WORKFLOWS / "continuous_learning_loop.py").read_text(encoding="utf-8")
    )

    zeroed = _zero_initialised_locals(tree)
    assert "workspaces_pending_training" in zeroed, (
        "the counter this rule was built around is no longer detected — the "
        "walk is broken, or the workflow was rewritten"
    )
    assert "workspaces_pending_training" in _mutated_names(tree)


def test_the_two_dead_fields_have_not_come_back() -> None:
    """Named explicitly because they were on the OUTPUT MODEL as well as
    the locals, and a Pydantic default of 0 is not a `name = 0` binding —
    the general rule above cannot see it."""
    source = (WORKFLOWS / "continuous_learning_loop.py").read_text(
        encoding="utf-8")

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if node.name != "ContinuousLearningLoopOutput":
            continue
        fields = {
            stmt.target.id
            for stmt in node.body
            if isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
        }
        assert "eval_regressions_detected" not in fields
        assert "workspaces_evaluated" not in fields
        return

    pytest.fail("ContinuousLearningLoopOutput is gone — update this test")


def test_the_exempt_list_has_not_gone_stale() -> None:
    stale = []
    for (module, name) in EXEMPT:
        path = WORKFLOWS / module
        if not path.exists():
            stale.append(f"{module} (file gone)")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if name in _mutated_names(tree) or name not in _zero_initialised_locals(tree):
            stale.append(f"{module}:{name}")

    assert stale == [], (
        "These EXEMPT entries no longer describe anything real:\n  "
        + "\n  ".join(stale)
    )
