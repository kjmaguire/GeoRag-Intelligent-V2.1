"""Guard against the recurring `str(x, site=...)` TypeError bug.

`bind_workspace_scope(conn, workspace_id=str(ws, site="..."))` is a copy-paste
error: the `site=` kwarg belongs to bind_workspace_scope, not str() — and
`str(obj, site=...)` raises TypeError at runtime on EVERY call (str's 2nd+ args
are encoding/errors). It silently broke trace writes, OCR persist, phase0
support_packet / tenant_isolation_auditor, and the SHAP writer until found in
the 2026-06-29 review. This test fails if the misplaced-kwarg pattern reappears.
"""

from __future__ import annotations

import re
from pathlib import Path

import app as _app_pkg

# Misplaced kwarg directly on str(): `str(<simple_arg>, <kwarg>=...)`.
# The first arg is restricted to a SIMPLE token (name/attr/subscript/quotes) so
# a nested call's own kwarg — e.g. str(well.get("X", value="")) — does NOT match
# (its first "arg" contains `(`, which is outside the char class). str() accepts
# only positional encoding/errors, so a keyword directly on str() is always a bug.
_BAD = re.compile(r"""\bstr\(\s*[\w.\[\]'"]+\s*,\s*[a-zA-Z_]\w*\s*=""")


def test_no_str_call_with_keyword_argument() -> None:
    app_root = Path(_app_pkg.__file__).resolve().parent
    offenders: list[str] = []
    for py in app_root.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue  # skip comments documenting the old bug
            if _BAD.search(line):
                offenders.append(f"{py.relative_to(app_root)}:{i}: {stripped[:100]}")

    assert offenders == [], (
        "Found str(...) calls with a keyword argument (the `str(x, site=...)` "
        "bug — the kwarg belongs to the OUTER call, not str()):\n  "
        + "\n  ".join(offenders)
    )
