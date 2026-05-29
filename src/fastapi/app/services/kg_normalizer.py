"""FastAPI shim for knowledge-graph name normalization.

Exposes the same normalizer functions as the Dagster-side
``georag_dagster.assets._name_normalization`` module so the FastAPI
agent tools can canonicalize user-supplied entity names before issuing
Cypher MATCH queries.

Keeping both services in sync
------------------------------
The FastAPI shim is intentionally a copy of the normalization logic rather
than a shared library import.  The two services run in different Python
environments and cannot import from each other.  The canonical test for
drift is ``src/fastapi/tests/test_kg_normalizer.py``, which runs a shared
fixture set against BOTH implementations and asserts identical output.

Design
------
All functions are pure (no I/O, no mutable state) and synchronous.  They
are safe to call from any async context — they don't need to be awaited.
Octane-safe: no module-level mutable state, no request-scoped singletons.

Public API
----------
normalize_formation_name(raw)  -> (canonical, original)
normalize_sample_name(raw)     -> (canonical, original)
normalize_report_title(raw)    -> (canonical, original)

Return value
------------
Each function returns a 2-tuple ``(canonical: str, original: str)`` so
callers always have access to both sides without extra bookkeeping.  The
canonical form is the value that should be used in Cypher MATCH parameters.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Internal helpers (mirrors _name_normalization.py in Dagster)
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[.,;:]+$")

_FORMATION_SUFFIXES = (
    "formation",
    "fm",
    "group",
    "gp",
    "member",
    "mbr",
    "zone",
    "unit",
    "suite",
    "complex",
    "sequence",
    "horizon",
    "bed",
    "beds",
    "assemblage",
)

_FORMATION_SUFFIX_PATTERN = re.compile(
    r"[\s\-]+" +
    r"(?:" + "|".join(re.escape(s) for s in _FORMATION_SUFFIXES) + r")" +
    r"\.?$"
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_formation_name(raw: str) -> tuple[str, str]:
    """Return (canonical_formation_name, original_name).

    Canonical form: lowercase, collapsed whitespace, stripped stratigraphic
    suffixes (fm, formation, group, gp, member, mbr, zone, unit, suite,
    complex, sequence, horizon, bed/beds, assemblage).

    This is the value that should be passed as a Cypher MATCH parameter
    when looking up Formation nodes by name.

    Args:
        raw: Raw formation name as supplied by the user or LLM.

    Returns:
        (canonical, original) 2-tuple.
    """
    original = raw
    if not raw or not raw.strip():
        return ("", original)

    s = _WS_RE.sub(" ", raw).strip()
    s = s.lower()
    s = _TRAILING_PUNCT_RE.sub("", s).strip()

    for _ in range(3):
        reduced = _FORMATION_SUFFIX_PATTERN.sub("", s).strip()
        if reduced == s or not reduced:
            break
        s = reduced

    s = _TRAILING_PUNCT_RE.sub("", s).strip()
    return (s, original)


def normalize_sample_name(raw: str) -> tuple[str, str]:
    """Return (canonical_sample_name, original_name).

    Canonical form: lowercase, collapsed whitespace, leading-zero segments
    stripped from hyphen-separated numeric parts.

    Args:
        raw: Raw sample identifier as supplied by the user or LLM.

    Returns:
        (canonical, original) 2-tuple.
    """
    original = raw
    if not raw or not raw.strip():
        return ("", original)

    s = _WS_RE.sub(" ", raw).strip().lower()

    parts = s.split("-")
    normalized_parts: list[str] = []
    for part in parts:
        if part.isdigit() and len(part) > 1:
            part = str(int(part))
        normalized_parts.append(part)
    s = "-".join(normalized_parts)

    return (s, original)


def normalize_report_title(raw: str) -> tuple[str, str]:
    """Return (canonical_report_title, original_title).

    Canonical form: lowercase + collapsed whitespace.  Punctuation is
    preserved — report titles require it for correct identification.

    Args:
        raw: Raw report title as supplied by the user or LLM.

    Returns:
        (canonical, original) 2-tuple.
    """
    original = raw
    if not raw or not raw.strip():
        return ("", original)

    s = _WS_RE.sub(" ", raw).strip().lower()
    return (s, original)
