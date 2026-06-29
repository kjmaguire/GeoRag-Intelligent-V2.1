"""FastAPI middleware sub-package.

The existing HTTP middleware stack (BodySizeLimitMiddleware,
GlobalTimeoutMiddleware, StructuredAccessLogMiddleware) lives in
``app/middleware.py`` (singular module).  This package holds new
async *helpers* that are called explicitly from route handlers —
not Starlette BaseHTTPMiddleware subclasses.

NOTE: Because this package directory shadows the ``app/middleware.py``
module, we re-export the HTTP middleware classes here so that
``from app.middleware import BodySizeLimitMiddleware`` works correctly
regardless of which form Python resolves ``app.middleware`` to.
"""

# Re-export HTTP middleware classes from the sibling module so that
# ``from app.middleware import BodySizeLimitMiddleware`` continues to work
# now that this package directory takes precedence over middleware.py.
import importlib as _importlib
import pathlib as _pathlib
import types as _types

_middleware_py = _pathlib.Path(__file__).parent.parent / "middleware.py"
_spec = _importlib.util.spec_from_file_location("app._middleware_impl", _middleware_py)
_mod: _types.ModuleType = _importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

BodySizeLimitMiddleware = _mod.BodySizeLimitMiddleware
GlobalTimeoutMiddleware = _mod.GlobalTimeoutMiddleware
StructuredAccessLogMiddleware = _mod.StructuredAccessLogMiddleware

# W3C Trace Context helpers (Module 10 Chunk 10.6). Still defined in the
# sibling middleware.py and used by StructuredAccessLogMiddleware — re-export
# them too so direct callers/tests can reach them through ``app.middleware``
# despite the package shadowing the module.
_is_valid_traceparent = _mod._is_valid_traceparent
_mint_traceparent = _mod._mint_traceparent

__all__ = [
    "BodySizeLimitMiddleware",
    "GlobalTimeoutMiddleware",
    "StructuredAccessLogMiddleware",
    "_is_valid_traceparent",
    "_mint_traceparent",
]
