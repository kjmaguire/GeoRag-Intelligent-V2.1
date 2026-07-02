"""Shared citation-marker patterns for the hallucination-prevention layers.

Single source of truth for what a citation marker looks like. Every layer
that strips, extracts, or matches citation markers must import from here —
the 2026-06-27 audit (T3) found three divergent copies of the pattern, and
the divergence was itself a bug class (layers 4 and 6 missed the colon form
and the PGEO prefix that layers 3 and the orchestrator validators accepted).

Marker grammar
--------------
- Prefixes: DATA, NI43, PUB, PGEO (corpus-scoped numeric markers), plus the
  ``ev`` evidence-id form used by the citation-first generator.
- Separators: the prompts instruct the model to emit the colon form
  (``[DATA:1]``, canonical per Kyle 2026-04-22); the response assembler
  appends the dash form (``[DATA-1]``). Both must be accepted for the
  duration of the rollout window.
- Citation objects (``Citation.citation_id``) are always dash-form — use
  ``canonical_marker()`` to normalise a text marker before comparing
  against them.
"""

from __future__ import annotations

import re

# Corpus prefixes for numeric citation markers ([DATA-1], [PGEO:4], ...).
CITATION_PREFIXES: frozenset[str] = frozenset({"DATA", "NI43", "PUB", "PGEO"})

_PREFIX_ALT = "|".join(sorted(CITATION_PREFIXES))

# Numeric citation marker, colon or dash form: [DATA:1], [NI43-2], [PGEO:4].
CITATION_MARKER_RE = re.compile(rf"\[(?:{_PREFIX_ALT})[:-]\d+\]")

# Capture variant — groups: (prefix, separator, index).
CITATION_MARKER_CAPTURE_RE = re.compile(rf"\[({_PREFIX_ALT})([:-])(\d+)\]")

# Superset that also matches [ev:abc1def2] evidence ids (non-numeric tail).
ALL_MARKER_RE = re.compile(rf"\[(?:{_PREFIX_ALT}|ev)[:-][A-Za-z0-9-]+\]")


def canonical_marker(prefix: str, index: str) -> str:
    """Dash-form marker matching ``Citation.citation_id`` (e.g. ``[DATA-1]``)."""
    return f"[{prefix}-{index}]"
