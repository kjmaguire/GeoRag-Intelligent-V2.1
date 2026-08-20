"""Every `_progress` call in a workflow must match the function it calls.

The bug this pins: `ingest_spatial`, `ingest_tabular` and `ingest_well_logs`
each wrote

    await _progress.mark_failed_by_run(run_id=run_id, error_text=str(exc))

but the parameter is named `error`. The call therefore raised

    TypeError: mark_failed_by_run() got an unexpected keyword argument 'error_text'

*inside the except block* - so the workflow reported a TypeError instead of
whatever actually went wrong, and the `silver.ingest_progress` row never
reached a terminal state, leaving the run stuck "in progress" in the UI
until the stale sweep timed it out with no explanation.

It survived because it only executes on the failure path, and the tests for
these three workflows only exercise the happy one. `error_text` is not even a
wrong guess: `_archive_progress.mark_terminal()` really does take
`error_text=`, so the two neighbouring modules disagree and a copy between
them is silently wrong.

Rather than test one call site, bind every `_progress`/`ingest_progress`
keyword call found in the workflow package against the real signature. A new
workflow with the same slip fails here without anyone remembering to add a
case.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.hatchet_workflows import _progress

WORKFLOW_DIR = Path(_progress.__file__).parent

#: Names the workflows import the progress module as.
_PROGRESS_ALIASES = {"_progress", "ingest_progress"}


def _keyword_calls() -> list[tuple[str, int, str, list[str]]]:
    """(file, line, function, keyword names) for every progress call."""
    found: list[tuple[str, int, str, list[str]]] = []
    for path in sorted(WORKFLOW_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not isinstance(fn, ast.Attribute) or not isinstance(fn.value, ast.Name):
                continue
            if fn.value.id not in _PROGRESS_ALIASES:
                continue
            # **kwargs forwarding has no literal names to check.
            names = [kw.arg for kw in node.keywords if kw.arg is not None]
            found.append((path.name, node.lineno, fn.attr, names))
    return found


def test_the_scan_finds_call_sites():
    """A silent zero here would make every assertion below vacuous."""
    calls = _keyword_calls()
    assert len(calls) > 10, f"expected many progress calls, found {len(calls)}"


@pytest.mark.parametrize(
    "filename,lineno,func_name,kwargs",
    _keyword_calls(),
    ids=lambda v: str(v) if not isinstance(v, list) else ",".join(v),
)
def test_progress_call_matches_its_signature(filename, lineno, func_name, kwargs):
    target = getattr(_progress, func_name, None)
    assert target is not None, (
        f"{filename}:{lineno} calls _progress.{func_name}(), which does not exist"
    )

    sig = inspect.signature(target)
    accepts_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    for name in kwargs:
        assert accepts_var_kw or name in sig.parameters, (
            f"{filename}:{lineno} passes {name!r} to {func_name}(), whose "
            f"parameters are {sorted(sig.parameters)}. This raises TypeError at "
            f"runtime - and on a failure path it replaces the real error."
        )


def test_mark_failed_by_run_takes_error_not_error_text():
    """The specific confusion, named.

    `_archive_progress.mark_terminal` takes `error_text`; this one takes
    `error`. Anyone reading one and writing the other gets a TypeError only
    when something has already gone wrong.
    """
    params = inspect.signature(_progress.mark_failed_by_run).parameters
    assert "error" in params
    assert "error_text" not in params
