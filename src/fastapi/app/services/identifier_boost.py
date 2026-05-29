"""Geological identifier detection for sparse-retrieval boosting.

Module 4 Phase B Chunk 3 -- B3 identifier-boost detection.

Purpose
-------
Identifier-heavy queries (e.g., "PLS-22-08 assay results" or "74I12 geology")
benefit from a wider sparse-candidate pool in Qdrant: a specific drillhole ID
is an exact-match token, not a semantic concept, so the SPLADE++ sparse branch
picks it up better than the dense branch.

When an identifier is detected, the orchestrator raises the Qdrant sparse
prefetch limit from the default 100 to 150 (SPARSE_BOOST_FACTOR = 1.5) so
more exact-token candidates enter the cross-store RRF pool.

Detection
---------
detect_identifiers(query) returns a DetectionResult dataclass:
    has_match:        bool -- True if any pattern class matched
    matched_patterns: list of pattern class names that fired
    matched_tokens:   deduplicated list of actual matched strings

Pattern classes
---------------
1. HOLE_ID_DASHED    -- "23-MS-117", "2024-DDH-001", "PLS-22-08"
2. HOLE_ID_COMPACT   -- "DDH0023", "MS2024001" (no dashes)
3. SAMPLE_ID_ALPHA   -- "MS240301", "AU123456" (letter prefix + digits)
4. SAMPLE_ID_DASHED  -- "AU-240301", "CU-123456" (2-letter prefix + dash + digits)
5. NTS_TILE          -- "74I12", "104B08" (Canadian NTS map tile codes)
6. COMMODITY_CODE    -- "Au", "U3O8", "REE" etc. (exact-match set, case-sensitive)

Workspace override path
-----------------------
TODO (Phase C): When workspace_settings gains an `identifier_patterns` field,
resolve_patterns(workspace_settings) should return the workspace-specific
compiled patterns instead of the defaults.  The field is not yet in the schema
(Module 9 scope) -- until then, get_patterns() returns defaults unconditionally.

Default-on
----------
Per Global Invariant 11, identifier boost is default-on.  The workspace
override `identifier_boost_disabled=True` would disable it, but that field
does not exist yet.  Boost is applied whenever detect_identifiers() returns
has_match=True.

Boost application
-----------------
The boost_factor is passed to hybrid_query() / hybrid_query_no_workspace() as
sparse_boost_factor.  Those functions multiply PREFETCH_LIMIT by the factor for
the sparse Prefetch branch only.  The dense branch is unchanged.

    Default prefetch per branch: 100
    Boosted sparse prefetch:     150 (100 * 1.5)

The dense branch stays at 100 so the RRF pool is deliberately sparse-heavy for
identifier queries without ballooning total candidates beyond reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Boost factor applied to the sparse prefetch limit when an identifier fires.
# ---------------------------------------------------------------------------

SPARSE_BOOST_FACTOR: float = 1.5


# ---------------------------------------------------------------------------
# Commodity code set (case-sensitive exact match)
# ---------------------------------------------------------------------------

_COMMODITY_CODES: frozenset[str] = frozenset({
    "Au", "Ag", "Cu", "Pb", "Zn", "Ni", "Co", "Mo",
    "U", "U3O8", "REE",
})


# ---------------------------------------------------------------------------
# Compiled regex patterns (compiled once at module import)
# ---------------------------------------------------------------------------

# Pattern 1: Dashed hole IDs.
# Covers common geology conventions:
#   "23-MS-117"    (digits-LETTERS-digits)
#   "2024-DDH-001" (digits-LETTERS-digits)
#   "PLS-22-08"    (LETTERS-digits-digits)
#   "AB-MS-005"    (LETTERS-LETTERS-digits)
#
# Two sub-patterns united with | to handle the two main structural families:
#
# Family A: letter-prefix, ANY middle (letters or digits), numeric suffix
#   e.g. PLS-22-08, AB-MS-005, DDH-2024-001
#   Pattern: [A-Z]{2,6} - [A-Z0-9]{1,6} - \d{1,5}
#   (requires a letter prefix so "2022-04-15" dates don't match)
#
# Family B: digit-prefix, letter-code middle, numeric suffix
#   e.g. 23-MS-117, 2024-DDH-001
#   Pattern: \d{2,4} - [A-Z]{1,6} - \d{1,5}
#   (the letter middle distinguishes it from ISO dates like 2022-04-15)
_RE_HOLE_ID_DASHED = re.compile(
    r"\b(?:"
    r"[A-Z]{2,6}-[A-Z0-9]{1,6}-\d{1,5}"   # Family A: letter prefix
    r"|"
    r"\d{2,4}-[A-Z]{1,6}-\d{1,5}"          # Family B: digit prefix + letter middle
    r")\b"
)

# Pattern 2: Compact hole IDs -- "DDH0023", "MS2024001" (no dashes)
# 2-4 uppercase letters immediately followed by 2-5 digits.
_RE_HOLE_ID_COMPACT = re.compile(
    r"\b[A-Z]{2,4}\d{2,5}\b"
)

# Pattern 3: Alpha-prefix sample IDs -- "MS240301", "AU123456"
# 1-4 uppercase letters + 4-8 digits (no dash).
_RE_SAMPLE_ID_ALPHA = re.compile(
    r"\b[A-Z]{1,4}\d{4,8}\b"
)

# Pattern 4: Dashed sample IDs -- "AU-240301", "CU-123456"
# 2 uppercase letters + dash + 6 digits.
_RE_SAMPLE_ID_DASHED = re.compile(
    r"\b[A-Z]{2}-\d{6}\b"
)

# Pattern 5: NTS map tile codes (Canadian National Topographic System)
# 2-3 digits + one letter A-P + 2 digits.  "74I12", "104B08".
_RE_NTS_TILE = re.compile(
    r"\b\d{2,3}[A-P]\d{2}\b"
)


@dataclass
class DetectionResult:
    """Result of identifier pattern detection on a query string.

    Attributes:
        has_match:        True if at least one identifier pattern matched.
        matched_patterns: Names of the pattern classes that fired.
        matched_tokens:   Deduplicated matched strings from the query text.
        boost_factor:     Recommended sparse prefetch multiplier.
                          1.0 when no match; SPARSE_BOOST_FACTOR when matched.
    """
    has_match: bool
    matched_patterns: list[str] = field(default_factory=list)
    matched_tokens: list[str] = field(default_factory=list)
    boost_factor: float = 1.0


def _check_commodity_codes(query: str) -> tuple[list[str], list[str]]:
    """Scan for exact commodity-code tokens in the query.

    Returns:
        Tuple of (matched_tokens, []) where the first element contains any
        commodity codes found.  The second element is unused (commodity codes
        have no sub-match to capture -- the word IS the token).
    """
    tokens: list[str] = []
    for code in _COMMODITY_CODES:
        # Require a word boundary around the code to avoid matching "Cub" for "Cu".
        pattern = rf"\b{re.escape(code)}\b"
        if re.search(pattern, query):
            tokens.append(code)
    return tokens


def detect_identifiers(query: str) -> DetectionResult:
    """Detect geological identifiers in a query string.

    Runs all compiled pattern classes against `query`.  Returns a
    DetectionResult summarising which classes fired and what they matched.

    Args:
        query: Raw natural-language query text from the user.

    Returns:
        DetectionResult.  has_match is True if any pattern class fired.
        boost_factor is SPARSE_BOOST_FACTOR when has_match is True,
        else 1.0 (no boost).

    Examples:
        >>> r = detect_identifiers("what are the results for PLS-22-08?")
        >>> r.has_match
        True
        >>> "HOLE_ID_DASHED" in r.matched_patterns
        True
        >>> "PLS-22-08" in r.matched_tokens
        True

        >>> r = detect_identifiers("how many drill holes are in the project?")
        >>> r.has_match
        False
    """
    all_patterns: list[str] = []
    all_tokens: list[str] = []
    seen_tokens: set[str] = set()

    def _add_matches(pattern_name: str, matches: list[str]) -> None:
        if matches:
            all_patterns.append(pattern_name)
            for m in matches:
                if m not in seen_tokens:
                    all_tokens.append(m)
                    seen_tokens.add(m)

    # Pattern 1: dashed hole IDs
    dashed_holes = _RE_HOLE_ID_DASHED.findall(query)
    _add_matches("HOLE_ID_DASHED", dashed_holes)

    # Pattern 2: compact hole IDs (only add if not already captured by dashed)
    compact_holes = _RE_HOLE_ID_COMPACT.findall(query)
    _add_matches("HOLE_ID_COMPACT", compact_holes)

    # Pattern 3: alpha-prefix sample IDs
    alpha_samples = _RE_SAMPLE_ID_ALPHA.findall(query)
    _add_matches("SAMPLE_ID_ALPHA", alpha_samples)

    # Pattern 4: dashed sample IDs
    dashed_samples = _RE_SAMPLE_ID_DASHED.findall(query)
    _add_matches("SAMPLE_ID_DASHED", dashed_samples)

    # Pattern 5: NTS tile codes
    nts_tiles = _RE_NTS_TILE.findall(query)
    _add_matches("NTS_TILE", nts_tiles)

    # Pattern 6: commodity codes (case-sensitive exact match)
    commodity_tokens = _check_commodity_codes(query)
    _add_matches("COMMODITY_CODE", commodity_tokens)

    has_match = bool(all_patterns)
    return DetectionResult(
        has_match=has_match,
        matched_patterns=all_patterns,
        matched_tokens=all_tokens,
        boost_factor=SPARSE_BOOST_FACTOR if has_match else 1.0,
    )


def get_patterns() -> dict[str, re.Pattern]:  # type: ignore[type-arg]
    """Return the compiled pattern dict (for inspection / override path).

    TODO (Phase C): When workspace_settings gains `identifier_patterns`,
    call resolve_patterns(workspace_settings) instead and merge the
    workspace-specific compiled patterns with or in place of these defaults.

    Returns:
        Dict mapping pattern class name to compiled re.Pattern.
    """
    return {
        "HOLE_ID_DASHED":   _RE_HOLE_ID_DASHED,
        "HOLE_ID_COMPACT":  _RE_HOLE_ID_COMPACT,
        "SAMPLE_ID_ALPHA":  _RE_SAMPLE_ID_ALPHA,
        "SAMPLE_ID_DASHED": _RE_SAMPLE_ID_DASHED,
        "NTS_TILE":         _RE_NTS_TILE,
        # COMMODITY_CODE is a frozenset, not a compiled regex -- returned for reference only.
    }
