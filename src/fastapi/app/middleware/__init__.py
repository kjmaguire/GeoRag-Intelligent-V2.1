"""FastAPI middleware sub-package.

``http`` holds the HTTP middleware stack (BodySizeLimitMiddleware,
GlobalTimeoutMiddleware, StructuredAccessLogMiddleware), re-exported here so
``from app.middleware import BodySizeLimitMiddleware`` works. The other
modules are async helpers called explicitly from route handlers, not
Starlette BaseHTTPMiddleware subclasses.

(Until 2026-10-06 the stack lived in a sibling ``app/middleware.py`` that
this package shadowed and loaded through ``importlib`` by file path.)
"""

from .http import (
    _PROBE_PATHS,
    BodySizeLimitMiddleware,
    GlobalTimeoutMiddleware,
    StructuredAccessLogMiddleware,
    _is_valid_traceparent,
    _mint_traceparent,
)

__all__ = [
    "BodySizeLimitMiddleware",
    "GlobalTimeoutMiddleware",
    "StructuredAccessLogMiddleware",
    "_PROBE_PATHS",
    "_is_valid_traceparent",
    "_mint_traceparent",
]
