"""Step 1 skeleton tests — assert each app.ocr module imports and exposes
the documented async function surface.

Per master-plan §3 Step 1: behavioural assertions are out of scope here.
Only the interface contract is locked. Step 3-6 add behaviour tests
inside this directory under names like ``test_ocr_preflight.py``,
``test_ocr_parse_native.py``, etc.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect

import pytest

OCR_MODULES = [
    ("app.ocr.preflight", "preflight"),
    ("app.ocr.profile", "profile"),
    ("app.ocr.parse_native", "parse_native"),
    ("app.ocr.parse_scanned", "parse_scanned"),
    ("app.ocr.parse_mixed", "parse_mixed"),
    ("app.ocr.parse_table_heavy", "parse_table_heavy"),
    ("app.ocr.render", "render_page"),
    ("app.ocr.quality_graph", "route_page"),
]

# Modules that are still Step 1 skeletons (raise NotImplementedError).
# As each implementation lands (doc-phase 51+), modules graduate out of
# this set. The behaviour tests for graduated modules live in
# `test_ocr_<modulename>.py` files.
SKELETON_MODULES: set[str] = set()  # All 8 modules graduated as of doc-phase 54.


@pytest.mark.parametrize("module_name,symbol_name", OCR_MODULES)
def test_ocr_module_imports(module_name: str, symbol_name: str) -> None:
    """Each §04p module must import and expose its documented symbol."""
    mod = importlib.import_module(module_name)
    sym = getattr(mod, symbol_name, None)
    assert sym is not None, (
        f"{module_name} missing expected symbol {symbol_name!r}"
    )
    assert callable(sym), f"{module_name}.{symbol_name} must be callable"
    assert inspect.iscoroutinefunction(sym), (
        f"{module_name}.{symbol_name} must be async (declared via `async def`)"
    )


def test_ocr_init_reexports() -> None:
    """app.ocr top-level __init__ re-exports the full surface."""
    import app.ocr as ocr

    expected = {
        "preflight",
        "profile",
        "parse_native",
        "parse_scanned",
        "parse_mixed",
        "parse_table_heavy",
        "render_page",
        "route_page",
    }
    actual = set(getattr(ocr, "__all__", []))
    missing = expected - actual
    assert not missing, f"app.ocr.__all__ missing: {missing}"
    for name in expected:
        assert hasattr(ocr, name), f"app.ocr missing top-level export {name!r}"


@pytest.mark.parametrize("module_name,symbol_name", OCR_MODULES)
def test_ocr_skeletons_raise_notimplemented(
    module_name: str, symbol_name: str
) -> None:
    """Skeleton stubs must raise NotImplementedError when invoked.

    Catches the failure mode where a skeleton accidentally has a real
    body that returns something — e.g. an empty dict — which would
    let downstream callers silently get bogus data when subsequent
    step implementations land elsewhere.

    Implemented modules are tracked in `SKELETON_MODULES` above and
    skipped here — behaviour tests for them live in `test_ocr_*.py`
    files.
    """
    if module_name not in SKELETON_MODULES:
        pytest.skip(f"{module_name} has graduated from skeleton — see SKELETON_MODULES")

    mod = importlib.import_module(module_name)
    fn = getattr(mod, symbol_name)
    sig = inspect.signature(fn)

    # Build dummy args matching positional parameters. Anything typed
    # `Path` gets `pathlib.Path("/nonexistent")`; everything else `None`.
    from pathlib import Path

    args = []
    for _param_name, param in sig.parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue  # has default → optional, skip
        if param.annotation is Path or "Path" in str(param.annotation):
            args.append(Path("/nonexistent"))
        elif "int" in str(param.annotation):
            args.append(0)
        elif "str" in str(param.annotation):
            args.append("native")
        else:
            args.append(None)

    with pytest.raises(NotImplementedError):
        asyncio.run(fn(*args))
