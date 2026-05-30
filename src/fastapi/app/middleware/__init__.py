"""FastAPI middleware sub-package.

The existing HTTP middleware stack (BodySizeLimitMiddleware,
GlobalTimeoutMiddleware, StructuredAccessLogMiddleware) lives in
``app/middleware.py`` (singular module).  This package holds new
async *helpers* that are called explicitly from route handlers —
not Starlette BaseHTTPMiddleware subclasses.
"""
