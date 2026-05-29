"""Unit tests for D3 rule-based follow-up generation."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent.followups import generate_followups


def _fake_response(text: str, confidence: float = 0.9):
    """Minimal GeoRAGResponse-shaped object — duck-typed for the generator."""
    return SimpleNamespace(text=text, confidence=confidence)


def _collar(hole_id: str, total_depth: float = 100.0):
    return SimpleNamespace(hole_id=hole_id, total_depth=total_depth)


class TestRefusalGuard:
    def test_returns_empty_on_refusal(self):
        response = _fake_response("I don't have data on that in this project.")
        assert generate_followups("q", response, []) == []

    def test_returns_empty_on_insufficient_language(self):
        response = _fake_response("insufficient information to answer.")
        assert generate_followups("q", response, []) == []

    def test_returns_empty_on_llm_unavailable(self):
        response = _fake_response("The language model is currently unavailable. Please try again.")
        assert generate_followups("q", response, []) == []


class TestHoleDeepDive:
    def test_offers_deep_dive_when_hole_cited_and_query_is_generic(self):
        from app.agent.tools import SpatialQueryResult

        collars = [_collar("XLS-24-10", 520.0), _collar("XLS-24-05", 250.0)]
        tool_results = [
            ("query_spatial_collars", SpatialQueryResult(
                collars=collars, count=len(collars), data_source="PostGIS silver.collars"
            )),
        ]
        response = _fake_response("The deepest hole is 520 m. [DATA-1]")
        result = generate_followups("what is the deepest hole?", response, tool_results)
        assert any("XLS-24-10" in r for r in result)

    def test_skips_when_query_already_names_a_hole(self):
        from app.agent.tools import SpatialQueryResult

        collars = [_collar("XLS-24-10", 520.0)]
        tool_results = [
            ("query_spatial_collars", SpatialQueryResult(
                collars=collars, count=1, data_source="PostGIS silver.collars"
            )),
        ]
        response = _fake_response("XLS-24-10 is 520 m deep. [DATA-1]")
        result = generate_followups("tell me about XLS-24-10", response, tool_results)
        # Should NOT repeat the hole the user already named.
        assert not any("XLS-24-10" in r for r in result)


class TestElementComparison:
    def test_offers_comparison_when_element_cited(self):
        from app.agent.tools import AssayDataResult

        tool_results = [
            ("query_assay_data", AssayDataResult(
                element="U3O8",
                samples=[],
                count=100,
                min_value=0.01, max_value=5.2, mean_value=0.37, median_value=0.18,
                available_elements=["U3O8", "Au"],
                data_source="PostGIS silver.samples",
            )),
        ]
        response = _fake_response("Mean U3O8 is 0.37% [DATA-1]")
        result = generate_followups("what is the mean uranium grade?", response, tool_results)
        assert any("U3O8" in r and "compare" in r.lower() for r in result)

    def test_skips_when_compare_already_in_query(self):
        from app.agent.tools import AssayDataResult

        tool_results = [
            ("query_assay_data", AssayDataResult(
                element="Au",
                samples=[],
                count=50,
                min_value=0.1, max_value=20.0, mean_value=2.3, median_value=1.8,
                available_elements=["Au"],
                data_source="PostGIS silver.samples",
            )),
        ]
        response = _fake_response("Mean Au grade across holes is 2.3 g/t [DATA-1]")
        result = generate_followups("compare mean Au grade across holes", response, tool_results)
        assert not any("compare" in r.lower() and "Au" in r for r in result)


class TestConfidenceProbe:
    def test_appends_source_probe_on_low_confidence(self):
        from app.agent.tools import SpatialQueryResult

        tool_results = [
            ("query_spatial_collars", SpatialQueryResult(
                collars=[_collar("A-1", 50.0)],
                count=1,
                data_source="PostGIS silver.collars",
            )),
        ]
        response = _fake_response("Based on [DATA-1]", confidence=0.3)
        result = generate_followups("what holes exist?", response, tool_results)
        assert any("sources" in r.lower() for r in result)

    def test_skips_source_probe_on_high_confidence(self):
        from app.agent.tools import SpatialQueryResult

        tool_results = [
            ("query_spatial_collars", SpatialQueryResult(
                collars=[_collar("A-1", 50.0)],
                count=1,
                data_source="PostGIS silver.collars",
            )),
        ]
        response = _fake_response("20 holes. [DATA-1]", confidence=0.95)
        result = generate_followups("how many holes?", response, tool_results)
        assert not any("sources was this answer based" in r.lower() for r in result)


class TestDeduplicationAndCap:
    def test_caps_at_three_suggestions(self):
        from app.agent.tools import SpatialQueryResult, AssayDataResult, DocumentSearchResult, GraphTraversalResult
        # Construct a rich context that would match every rule.
        tool_results = [
            ("query_spatial_collars", SpatialQueryResult(
                collars=[_collar("H-1", 100.0)], count=1, data_source="pg"
            )),
            ("query_assay_data", AssayDataResult(
                element="Au", samples=[], count=10,
                min_value=0.1, max_value=2.0, mean_value=1.0, median_value=0.8,
                available_elements=["Au"], data_source="pg",
            )),
            ("traverse_knowledge_graph", GraphTraversalResult(
                entities=[SimpleNamespace(name="Triple R deposit")],
                count=1,
                data_source="Neo4j",
            )),
            ("search_documents", DocumentSearchResult(
                chunks=[SimpleNamespace()], count=1, data_source="Qdrant",
            )),
        ]
        response = _fake_response("an answer [DATA-1]", confidence=0.4)  # low enough to add source probe too
        result = generate_followups("what's going on here?", response, tool_results)
        assert len(result) <= 3


class TestEmptyPaths:
    def test_empty_when_no_tool_results(self):
        response = _fake_response("Bland answer", confidence=0.9)
        assert generate_followups("q", response, []) == []

    def test_empty_when_response_is_none(self):
        assert generate_followups("q", None, []) == []
