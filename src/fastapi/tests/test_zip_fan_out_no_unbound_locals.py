"""No branch of the ZIP fan-out may read a local that only a SIBLING binds.

WHY THIS FILE EXISTS
    ``_ingest_one`` dispatches one archive member through a nine-arm
    ``if/elif`` chain. Each arm that stores something to bronze assigns its
    own ``safe_name`` and reads its own bytes, because the arms are mutually
    exclusive -- reaching one proves none of the others ran.

    The standalone-dBASE arm, added so a loose ``.dbf``/``.dat`` inside a ZIP
    reaches ingest_tabular, read ``safe_name`` and ``file_bytes`` without
    assigning either. Both are bound only in earlier arms, so the branch
    raised ``UnboundLocalError`` every single time it was taken.

    What makes this worth a permanent guard rather than a one-line fix is
    how the failure presented. The per-file ``try/except`` at the call site
    catches the exception, increments ``counts['errors']`` and moves on. The
    archive finishes and is marked ``partial`` with a generic "N of M files
    failed" message. There is no bronze object, no ingest_tabular dispatch
    and no ingest_progress row -- so the file has no line on the Ingestion
    Runs page at all. To the geologist it simply is not there, which is the
    "my file vanished" symptom, landing on the exact format the branch was
    written to support.

    A unit test would have to construct a ZIP and a live store to catch it.
    The property is structural, so this checks it structurally -- the same
    approach test_ingest_tabular_typed_write_reachable.py takes to the dead
    typed writes found the same day.

WHAT THIS DOES NOT CATCH
    A name bound in an ``if`` nested INSIDE one arm but read after it, and
    anything reached through a helper. This pins the specific shape that has
    actually bitten: arm reads what only a sibling arm assigns.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULE = (
    Path(__file__).resolve().parents[1]
    / "app" / "hatchet_workflows" / "ingest_zip_archive.py"
)
FUNCTION = "_ingest_one"


def _assigned(node: ast.AST) -> set[str]:
    """Every bare name this subtree binds."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Assign):
            out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(
            n, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor),
        ) and isinstance(n.target, ast.Name):
            # All four bind a single `target`, so one arm covers them.
            out.add(n.target.id)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if isinstance(item.optional_vars, ast.Name):
                    out.add(item.optional_vars.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            out |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
    return out


def _loaded(node: ast.AST) -> set[str]:
    return {
        n.id for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def _chain(fn: ast.AST) -> tuple[list[tuple[str, list[ast.stmt]]], set[str]]:
    """The top-level if/elif arms of `fn`, plus names bound before the chain."""
    head = next((s for s in fn.body if isinstance(s, ast.If)), None)
    assert head is not None, f"{FUNCTION} no longer opens with an if/elif chain"

    pre: set[str] = set()
    for stmt in fn.body:
        if stmt is head:
            break
        pre |= _assigned(stmt)

    arms: list[tuple[str, list[ast.stmt]]] = []
    cur: ast.If | None = head
    while cur is not None:
        arms.append((ast.unparse(cur.test)[:70], cur.body))
        rest = cur.orelse
        if len(rest) == 1 and isinstance(rest[0], ast.If):
            cur = rest[0]
        else:
            if rest:
                arms.append(("else", rest))
            cur = None
    return arms, pre


@pytest.fixture(scope="module")
def function() -> ast.AST:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == FUNCTION),
        None,
    )
    assert fn is not None, f"{FUNCTION} not found — re-aim this guard"
    return fn


def test_the_chain_is_still_the_shape_this_guard_assumes(function: ast.AST) -> None:
    # If the dispatch is refactored into something else, this file must be
    # re-aimed rather than passing forever on a structure that is gone.
    arms, _ = _chain(function)
    assert len(arms) >= 6, (
        f"expected the format-dispatch chain, found {len(arms)} arm(s)"
    )


def test_no_arm_reads_a_local_only_a_sibling_arm_binds(function: ast.AST) -> None:
    arms, pre = _chain(function)
    params = {
        a.arg for a in function.args.args + function.args.kwonlyargs
    }

    everywhere: set[str] = set()
    for _, body in arms:
        for stmt in body:
            everywhere |= _assigned(stmt)

    problems: list[str] = []
    for label, body in arms:
        own: set[str] = set()
        read: set[str] = set()
        for stmt in body:
            own |= _assigned(stmt)
            read |= _loaded(stmt)
        risky = sorted((read & everywhere) - own - pre - params)
        if risky:
            problems.append(f"`{label}` reads {risky}")

    assert not problems, (
        "these arms read locals that only a SIBLING arm assigns, so taking "
        "them raises UnboundLocalError. The per-file try/except turns that "
        "into counts['errors'] and a generic 'N of M files failed', with no "
        "bronze object and no ingest_progress row — the file vanishes.\n  "
        + "\n  ".join(problems)
    )
