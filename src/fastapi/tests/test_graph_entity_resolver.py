"""Eval 14 R3 follow-up — entity disambiguation helper unit tests.

The Neo4j-dependent paths require a live graph and are gated under
@pytest.mark.integration. These tests cover the regex extraction +
the degrade-gracefully contract.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.graph_entity_resolver import (
    extract_candidate_terms,
    resolve_formation_terms,
)


class TestExtractCandidateTerms:
    def test_athabasca_sandstone(self) -> None:
        terms = extract_candidate_terms(
            "What is the thickness of the Athabasca Sandstone here?"
        )
        assert any("Athabasca Sandstone" in t for t in terms)

    def test_zone_pattern(self) -> None:
        terms = extract_candidate_terms(
            "Show me drillholes that intersect Zone 3 and Zone B."
        )
        assert "Zone 3" in terms
        assert "Zone B" in terms

    def test_the_x_zone_pattern(self) -> None:
        terms = extract_candidate_terms(
            "What are the grades in the Main zone vs the Eastern zone?"
        )
        assert any(t == "Main" for t in terms)
        assert any(t == "Eastern" for t in terms)

    def test_no_candidates_returns_empty(self) -> None:
        # Generic numeric query has no formation reference.
        assert extract_candidate_terms("How many drill holes?") == []

    def test_dedupe_case_insensitive(self) -> None:
        terms = extract_candidate_terms(
            "Tell me about the Athabasca Sandstone. The Athabasca "
            "Sandstone hosts uranium."
        )
        normalised = [t.lower() for t in terms]
        # No duplicate entries.
        assert len(normalised) == len(set(normalised))


class TestResolveDegradeGracefully:
    @pytest.mark.asyncio
    async def test_no_driver_returns_empty(self) -> None:
        result = await resolve_formation_terms(None, "Athabasca Sandstone")
        assert result == []

    @pytest.mark.asyncio
    async def test_no_candidates_skips_neo4j_entirely(self) -> None:
        driver = MagicMock()
        # Should not even open a session because no candidates extracted.
        result = await resolve_formation_terms(driver, "How many holes?")
        assert result == []
        driver.session.assert_not_called()

    @pytest.mark.asyncio
    async def test_neo4j_exception_returns_empty_not_raise(self) -> None:
        """Graph outage must NOT block the chat — degrade to empty."""
        driver = MagicMock()
        driver.session = MagicMock(side_effect=ConnectionError("neo4j down"))

        # Should never raise.
        result = await resolve_formation_terms(
            driver, "the Athabasca Sandstone",
        )
        assert result == []
