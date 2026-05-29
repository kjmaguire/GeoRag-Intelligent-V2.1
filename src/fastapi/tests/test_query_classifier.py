"""Unit tests for the spec-aligned query classifier.

Module 4 Phase B Chunk 1 — B3 validation.

Coverage requirements per task spec:
    - 5–8 representative queries per class
    - ≥ 30 tests total
    - All six spec classes covered (factual, spatial, document, computation,
      viz, unknown)

Test strategy:
    - Pure unit tests: no DB, no LLM, no Redis, no network.
    - Parametrized pytest cases for fast exhaustive coverage.
    - Canonical geological domain queries representative of real user patterns
      from the GeoRAG golden-query set.
    - Precedence tests: verify viz > spatial > computation > document > factual.

Run inside the container:
    docker exec georag-fastapi pytest tests/test_query_classifier.py -v
"""

from __future__ import annotations

import pytest

from app.services.query_classifier import (
    RETRIEVAL_STRATEGY_VERSION,
    classify_query,
)


# ---------------------------------------------------------------------------
# VIZ class — explicit rendering verbs (highest precedence)
# ---------------------------------------------------------------------------

VIZ_QUERIES = [
    # Stereonet
    "Plot a stereonet of the structural measurements from PLS-22.",
    # Rose diagram
    "Plot a rose diagram of the fault orientations.",
    # Collar map render
    "Render a map of all drill hole collars in the project.",
    # Cross-section view
    "Render the cross section through the Triple R deposit.",
    # Heat map
    "Generate a map showing the heat map of alteration intensity.",
    # Contour
    "Display contour lines of the top-of-basement surface.",
    # Chart
    "Plot the grade distribution as a chart.",
    # Wireframe
    "Generate a wireframe of the mineralized zone.",
]


@pytest.mark.parametrize("query", VIZ_QUERIES)
def test_classify_viz(query: str) -> None:
    result = classify_query(query)
    assert result == "viz", (
        f"Expected 'viz' for: {query!r}  got: {result!r}"
    )


# ---------------------------------------------------------------------------
# SPATIAL class
# ---------------------------------------------------------------------------

SPATIAL_QUERIES = [
    # Basic collar inventory with drill-hole domain terms
    "How many drill holes are in project PLS?",
    # Geographic proximity
    "Which drill holes are within 500 metres of the Triple R deposit?",
    # Collar coordinates
    "What are the coordinates of collar PLS-22-08?",
    # Inventory count with drill-hole context
    "How many drillholes are in the active campaign?",
    # Azimuth / dip lookup
    "What is the azimuth and dip of hole DDH-2024-01?",
    # Spatial proximity with distance
    "Find all collars within a 1 km radius of easting 450000 northing 6200000.",
    # UTM/CRS
    "What UTM zone is used for the project coordinates?",
    # Drill hole locations along section
    "List the drill hole locations along the cross section line.",
]


@pytest.mark.parametrize("query", SPATIAL_QUERIES)
def test_classify_spatial(query: str) -> None:
    result = classify_query(query)
    assert result == "spatial", (
        f"Expected 'spatial' for: {query!r}  got: {result!r}"
    )


# ---------------------------------------------------------------------------
# COMPUTATION class
# ---------------------------------------------------------------------------

COMPUTATION_QUERIES = [
    # Grade calculation
    "What is the average uranium grade in the measured resource?",
    # Tonnage
    "What is the total indicated tonnage at a 0.1% eU3O8 cutoff?",
    # Weighted average
    "Calculate the weighted average grade for all assay samples in PLS-22.",
    # Grade-thickness
    "Compute the grade-thickness product for the Spitfire zone.",
    # Highest grade
    "What is the highest grade intercept in the deposit?",
    # Geochem mean
    "What is the mean uranium concentration across all geochem samples?",
    # Resource estimate
    "What is the inferred resource tonnage at 0.05% eU3O8 cut-off grade?",
    # Median grade
    "What is the median gold grade in the indicated resource category?",
]


@pytest.mark.parametrize("query", COMPUTATION_QUERIES)
def test_classify_computation(query: str) -> None:
    result = classify_query(query)
    assert result == "computation", (
        f"Expected 'computation' for: {query!r}  got: {result!r}"
    )


# ---------------------------------------------------------------------------
# DOCUMENT class
# ---------------------------------------------------------------------------

DOCUMENT_QUERIES = [
    # NI 43-101 report
    "What does the NI 43-101 report say about the deposit type?",
    # Report filing
    "When was the NI 43-101 report filed on SEDAR?",
    # Report conclusion section
    "What does the conclusion section of the technical report say?",
    # Report recommendations section
    "What is in the recommendations section of the 2024 technical report?",
    # Data verification appendix
    "What is in the data verification appendix of the PLS report?",
    # Geological setting in report
    "What is the geological setting described in the technical report?",
    # Technical report content
    "What does the introduction section of the technical report contain?",
    # Geological study
    "What did the 2023 geological study conclude about the unconformity?",
]


@pytest.mark.parametrize("query", DOCUMENT_QUERIES)
def test_classify_document(query: str) -> None:
    result = classify_query(query)
    assert result == "document", (
        f"Expected 'document' for: {query!r}  got: {result!r}"
    )


# ---------------------------------------------------------------------------
# FACTUAL class
# ---------------------------------------------------------------------------

FACTUAL_QUERIES = [
    # Deposit type entity (factual phrase: "uranium deposit style")
    "What is the uranium deposit style at the PLS project?",
    # Knowledge graph query (factual token: "knowledge graph")
    "What entities are connected to the Triple R node in the knowledge graph?",
    # Entity relationship (factual phrase: "associated with the")
    "What geological formations are associated with the mineralization at depth?",
    # Pathfinder elements (factual token: "pathfinder")
    "What pathfinder elements are used in uranium exploration?",
    # Deposit hosted relationship (factual phrase: "hosted by")
    "What structure is the mineralization hosted by at Triple R?",
    # Gold deposit style (factual phrase: "gold deposit style")
    "What is the gold deposit style at the prospect?",
]


@pytest.mark.parametrize("query", FACTUAL_QUERIES)
def test_classify_factual(query: str) -> None:
    result = classify_query(query)
    assert result == "factual", (
        f"Expected 'factual' for: {query!r}  got: {result!r}"
    )


# ---------------------------------------------------------------------------
# UNKNOWN class (out-of-scope queries)
# ---------------------------------------------------------------------------

UNKNOWN_QUERIES = [
    # Pure gibberish
    "Xlkjfq zxcvbn asdfgh?",
    # Completely off-topic geography
    "What is the capital of France?",
    # Non-geological tech
    "How do I install Python packages?",
    # Sports
    "Who won the Stanley Cup in 2024?",
    # Cooking — no geological tokens
    "Tell me how to make pasta.",
]


@pytest.mark.parametrize("query", UNKNOWN_QUERIES)
def test_classify_unknown(query: str) -> None:
    result = classify_query(query)
    assert result == "unknown", (
        f"Expected 'unknown' for: {query!r}  got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Precedence tests — verify viz > spatial > computation > document > factual
# ---------------------------------------------------------------------------


def test_viz_beats_spatial() -> None:
    """A plot request for drill hole data → viz wins (viz > spatial)."""
    query = "Plot the drill hole collars on a map."
    result = classify_query(query)
    assert result == "viz"


def test_viz_beats_computation() -> None:
    """A plot request for grade data → viz wins (viz > computation)."""
    query = "Plot the grade distribution histogram for assay samples."
    result = classify_query(query)
    assert result == "viz"


def test_spatial_beats_computation() -> None:
    """A drill-hole proximity query with grade context → spatial wins."""
    query = "Which drill holes are within 500m of the highest grade intercept?"
    result = classify_query(query)
    assert result == "spatial"


def test_spatial_beats_document() -> None:
    """A collar-count query that mentions a report → spatial wins."""
    query = "How many drill holes are listed as drillholes in the 43-101 report?"
    result = classify_query(query)
    assert result == "spatial"


def test_computation_beats_document() -> None:
    """Resource estimate calculation from a report → computation wins."""
    query = "Calculate the weighted average grade from the resource estimate in the technical report."
    result = classify_query(query)
    assert result == "computation"


def test_document_beats_factual() -> None:
    """A factual question explicitly about a report section → document wins."""
    query = "What does the technical report say about the geological setting?"
    result = classify_query(query)
    assert result == "document"


# ---------------------------------------------------------------------------
# RETRIEVAL_STRATEGY_VERSION constant checks
# ---------------------------------------------------------------------------


def test_retrieval_strategy_version_format() -> None:
    """RETRIEVAL_STRATEGY_VERSION must follow a recognised versioning pattern.

    Drift fix (10.1): the original regex `^v\\d+-[\\w-]+-\\d{4}-\\d{2}-\\d{2}$`
    rejected 'v3.1-think-off-2026-04-21' because of the dot in 'v3.1'. The
    version scheme was deliberately extended to allow minor bumps (vN.M-suffix-date)
    without requiring a full RETRIEVAL_STRATEGY_VERSION rename. The updated regex
    accepts both vN-suffix-date and vN.M-suffix-date forms.
    """
    import re
    # Accepts: v3-foo-2026-04-21, v3.1-foo-bar-2026-04-21, etc.
    pattern = r"^v\d+(\.\d+)?-[\w][\w-]+-\d{4}-\d{2}-\d{2}$"
    assert re.match(pattern, RETRIEVAL_STRATEGY_VERSION), (
        f"RETRIEVAL_STRATEGY_VERSION={RETRIEVAL_STRATEGY_VERSION!r} does not match "
        f"expected format {pattern!r}"
    )


def test_retrieval_strategy_version_post_b4() -> None:
    """After Module 5 Phase B the version must be v3 or later (never the old v1/v2 strings).

    Drift fix (10.1): the original test asserted 'hybrid' in the version string
    because the version was v1-hybrid-2026-04-21 when that test was written.
    The version was subsequently bumped to v3/v3.1 as the model stack evolved
    (Qwen 3 MoE, think-off) — 'hybrid' was intentionally dropped from the name
    because SPLADE+RRF is now the standard pipeline, not a named variant.
    We now assert version is v3 or later rather than looking for 'hybrid'.
    """
    import re
    # Extract the numeric major version (e.g. '3' from 'v3.1-think-off-2026-04-21').
    m = re.match(r"^v(\d+)", RETRIEVAL_STRATEGY_VERSION)
    assert m is not None, (
        f"Cannot parse major version from {RETRIEVAL_STRATEGY_VERSION!r}"
    )
    major = int(m.group(1))
    assert major >= 3, (
        f"Expected RETRIEVAL_STRATEGY_VERSION major >= 3 (post-Module-5 baseline), "
        f"got v{major} from {RETRIEVAL_STRATEGY_VERSION!r}. "
        "If the version was intentionally rolled back, update this test."
    )


# ---------------------------------------------------------------------------
# Return type validation
# ---------------------------------------------------------------------------


def test_return_type_is_valid_literal() -> None:
    """classify_query must always return a value from QueryClassLiteral."""
    valid_classes = {"factual", "spatial", "document", "computation", "viz", "unknown"}
    test_inputs = [
        "drill hole count",
        "average grade",
        "Plot the map.",
        "ni 43-101 report",
        "geological setting",
        "xlkjfqzxcvbn",
        "",  # empty string edge case → unknown
    ]
    for query in test_inputs:
        result = classify_query(query)
        assert result in valid_classes, (
            f"classify_query({query!r}) returned {result!r} which is not in {valid_classes}"
        )


def test_empty_string_returns_unknown() -> None:
    """An empty query string must return 'unknown'."""
    assert classify_query("") == "unknown"
