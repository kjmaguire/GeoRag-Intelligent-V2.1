"""The typed writes on the dBASE path must be REACHABLE for a dBASE file.

WHY THIS FILE EXISTS
    On 2026-08-25 the whole typed-write block -- collars, surveys and
    surface geochemistry -- sat one indent level too deep, inside

        for access_name, access_rows in access_layers:

    ``access_layers`` is appended to in exactly one place, inside
    ``if suffix in ACCESS_EXTENSIONS`` (``.mdb``/``.accdb``). A MapInfo
    ``.dat`` takes the mutually exclusive ``elif suffix in
    DBASE_EXTENSIONS`` branch, so for the file this code was written for
    the list is empty, the loop body never runs, and every typed write is
    dead.

    The failure is silent in the worst way. The attribute copy is written
    BEFORE the loop, so the run reports success with a real row count, no
    warning fires, and the only symptom is that Workspace 3D is empty --
    which reads as a broken viewer, not as an ingest that skipped its own
    output.

    The block was plainly written for the dBASE path: every statement in
    it reads ``attribute_rows``, the dBASE variable, and never
    ``access_rows``, the loop variable. So it was not a design choice
    being reversed here, it was an indentation slip.

WHY THIS TEST IS STRUCTURAL RATHER THAN BEHAVIOURAL
    Unit tests did not and could not catch it. Nineteen of them, in
    test_ingest_tabular_discover_trace.py, call ``_collapse_discover_traces``
    and ``_trace_survey_stations`` DIRECTLY. Every one passed against the
    dead code, because a pure function does not care whether anything
    calls it. Proving the workflow reaches them needs either a live
    Postgres and a full Hatchet context, or a look at the call graph.

    This file takes the second option, which is the same approach
    test_bind_workspace_scope_guard.py and its siblings already use for
    structural invariants that are quietly wrong rather than loudly
    broken.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

MODULE = (
    Path(__file__).resolve().parents[1]
    / "app" / "hatchet_workflows" / "ingest_tabular.py"
)

#: The entry points that turn a dBASE table into typed geological rows.
#: Each one is the head of a branch that writes to a different silver table.
TYPED_WRITE_CALLS = (
    "_discover_trace_columns",
    "_collapse_discover_traces",
    "_trace_survey_stations",
    "_write_collars",
    "_surface_geochem_columns",
    "_write_surface_geochem",
)


@pytest.fixture(scope="module")
def tree() -> ast.Module:
    return ast.parse(MODULE.read_text(encoding="utf-8"))


def _calls_in(node: ast.AST) -> set[str]:
    return {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def _access_layer_loops(tree: ast.Module) -> list[ast.For]:
    return [
        n for n in ast.walk(tree)
        if isinstance(n, ast.For)
        and isinstance(n.iter, ast.Name)
        and n.iter.id == "access_layers"
    ]


def test_the_access_layers_loop_exists_and_this_test_is_still_aimed_at_it(
    tree: ast.Module,
) -> None:
    # If the loop is renamed or removed this file must be re-aimed rather
    # than silently passing forever on an invariant it no longer checks.
    loops = _access_layer_loops(tree)
    assert len(loops) == 1, (
        "expected exactly one `for ... in access_layers:` loop; the guard "
        "below is written against it. Re-aim this test."
    )


def test_no_typed_write_is_trapped_inside_the_access_layers_loop(
    tree: ast.Module,
) -> None:
    loop = _access_layer_loops(tree)[0]
    trapped = sorted(_calls_in(loop) & set(TYPED_WRITE_CALLS))

    assert not trapped, (
        f"{trapped} are nested inside `for ... in access_layers:` "
        f"(lines {loop.lineno}-{loop.end_lineno}). That list is only ever "
        "populated for .mdb/.accdb, so on the dBASE path the loop does not "
        "iterate and these writes are DEAD. A .dat ingest then reports "
        "success with its attribute rows while silver.collars, "
        "silver.surveys and silver.geochemistry stay empty, and no warning "
        "says so."
    )


def test_the_access_loop_still_writes_its_own_attribute_layers(
    tree: ast.Module,
) -> None:
    # The dedent that fixes the above must not go one line too far and
    # carry the Access fan-out out with it: one Access table -> one
    # attribute_tables layer is the whole reason that loop exists.
    loop = _access_layer_loops(tree)[0]
    assert "_write_attribute_rows" in _calls_in(loop), (
        "the access_layers loop no longer writes attribute rows — the "
        "dedent took the fan-out with it, and a 19-table Access database "
        "would land as nothing."
    )


@pytest.mark.parametrize("call", TYPED_WRITE_CALLS)
def test_every_typed_write_is_still_called_somewhere(
    tree: ast.Module, call: str,
) -> None:
    # A dedent that overshoots can push a branch out of the `elif suffix in
    # DBASE_EXTENSIONS` arm entirely, which trades a silent no-op for a
    # NameError on attribute_rows. Cheap to pin, and it fails loudly.
    assert call in _calls_in(tree), f"{call} is defined but never called"


def test_the_typed_writes_read_the_dbase_rows_not_the_access_loop_variable(
    tree: ast.Module,
) -> None:
    """The evidence that this block belongs on the dBASE path.

    ``_collapse_discover_traces``, ``_trace_survey_stations`` and
    ``_write_surface_geochem`` are all handed ``attribute_rows``. If one
    of them is ever switched to ``access_rows`` it has been moved back
    inside the loop, and the bug is back with a different shape.
    """
    source = MODULE.read_text(encoding="utf-8")
    call_tree = ast.parse(source)

    checked = 0
    for node in ast.walk(call_tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in {
            "_collapse_discover_traces",
            "_trace_survey_stations",
            "_write_surface_geochem",
        }:
            continue
        arg_names = {
            a.id for a in ast.walk(node) if isinstance(a, ast.Name)
        }
        assert "access_rows" not in arg_names, (
            f"{node.func.id} at line {node.lineno} reads `access_rows`; the "
            "typed writes operate on the dBASE table's `attribute_rows`."
        )
        checked += 1

    assert checked >= 3, (
        f"only found {checked} typed-write call sites to check — the block "
        "may have been restructured and this guard is no longer looking at it"
    )
