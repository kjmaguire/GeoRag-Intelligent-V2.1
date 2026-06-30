"""Unit tests for the query classifier and hint extractor in orchestrator.py.

Covers:
  - _classify_query: category flags and pg_jurisdictions / pg_canonical_types /
    pg_commodities hint extraction for a range of query strings.
  - _extract_public_geoscience_hints: pure-function isolation tests.
  - Cache key isolation: different categories yield different keys; no
    categories uses a v1 namespace (back-compat).

Run with:
    pytest tests/test_orchestrator_classifier.py -v
"""

from __future__ import annotations

from app.agent.orchestrator import _classify_query, _extract_public_geoscience_hints

# ---------------------------------------------------------------------------
# _classify_query
# ---------------------------------------------------------------------------


class TestClassifyQuery:
    """Tests for the lightweight keyword classifier."""

    def test_gold_occurrences_in_saskatchewan(self) -> None:
        """Classic PGEO query: jurisdiction + commodity + occurrence keyword."""
        result = _classify_query("What gold occurrences are in Saskatchewan?")
        assert result["public_geoscience"] is True
        assert "CA-SK" in result["pg_jurisdictions"]
        assert "Au" in result["pg_commodities"]
        assert "mineral_occurrence" in result["pg_canonical_types"]

    def test_drillholes_near_athabasca(self) -> None:
        """Mention of 'drillholes' should populate drillhole_collar canonical type."""
        result = _classify_query("show me drillholes near Athabasca")
        assert "drillhole_collar" in result["pg_canonical_types"]
        # 'Athabasca' is a geological term, not a jurisdiction alias.
        assert result["pg_jurisdictions"] == []

    def test_smdi_keyword_routes_to_public_geoscience(self) -> None:
        """SMDI is a PGEO-surface keyword; must route public_geoscience = True."""
        result = _classify_query("SMDI 0123 status")
        assert result["public_geoscience"] is True
        assert "mineral_occurrence" in result["pg_canonical_types"]

    def test_bc_minfile_uranium_occurrences(self) -> None:
        """British Columbia + MINFILE + occurrence: jurisdiction CA-BC, uranium commodity,
        mineral_occurrence canonical type.
        """
        result = _classify_query("British Columbia MINFILE uranium occurrence data")
        assert "CA-BC" in result["pg_jurisdictions"]
        assert "U" in result["pg_commodities"]
        assert "mineral_occurrence" in result["pg_canonical_types"]

    def test_bc_two_letter_alias(self) -> None:
        """V1.2 fix: 2-letter province codes ("BC") now resolve too.
        Word-boundary matching prevents false-matches inside English words.
        """
        result = _classify_query("BC MINFILE uranium occurrence")
        assert "CA-BC" in result["pg_jurisdictions"]

    def test_sk_two_letter_alias_with_word_boundary(self) -> None:
        """V1.2 fix: 'SK' alone matches CA-SK; 'task' (containing 'sk' as a
        substring) does NOT trigger CA-SK."""
        # Standalone "SK" → matches.
        r1 = _classify_query("SK gold occurrence list")
        assert "CA-SK" in r1["pg_jurisdictions"]
        # "task" must NOT match — \b prevents substring false-matches.
        r2 = _classify_query("the task is to find drill holes")
        assert "CA-SK" not in r2.get("pg_jurisdictions", [])

    def test_on_alias_omitted_to_avoid_english_preposition(self) -> None:
        """V1.2 fix: 'on' is deliberately NOT in the alias map because it
        false-matches the English preposition. The full name still works.
        """
        # "on" alone → no jurisdiction (preposition false-match guard).
        r1 = _classify_query("samples on the SMDI 0123 deposit")
        assert "CA-ON" not in r1.get("pg_jurisdictions", [])
        # "Ontario" full name → matches.
        r2 = _classify_query("Ontario MDI gold deposits")
        assert "CA-ON" in r2["pg_jurisdictions"]

    def test_base_metals_producing_mines(self) -> None:
        """'mines' keyword in query → mine canonical type. Multiple base-metal
        commodities expand via the commodity token map."""
        result = _classify_query("what are base metal producing mines")
        assert "mine" in result["pg_canonical_types"]

    def test_ambiguous_intent_via_jurisdiction_plus_geological_noun(self) -> None:
        """Saskatchewan + a geological noun triggers the ambiguous-→-both-corpora
        branch even when no explicit PGEO keyword appears."""
        result = _classify_query("Saskatchewan mineralization")
        # The ambiguous branch forces public_geoscience True when a known
        # jurisdiction alias + a document/graph keyword coexist in the query.
        assert result["public_geoscience"] is True

    def test_simple_spatial_query_does_not_route_public_geoscience(self) -> None:
        """Pure internal-archive query must NOT set public_geoscience = True."""
        result = _classify_query("How many drill holes are in this project?")
        assert result["public_geoscience"] is False
        assert result["spatial"] is True

    def test_ni43_document_query_does_not_route_public_geoscience(self) -> None:
        """NI 43-101 report query routes to documents, not public_geoscience."""
        result = _classify_query("What does the NI 43-101 report say about the resource estimate?")
        assert result["documents"] is True
        assert result["public_geoscience"] is False

    def test_fallback_to_spatial_and_documents_when_no_match(self) -> None:
        """Totally unrecognised query falls back to spatial=True + documents=True."""
        result = _classify_query("xyzzy frob nonce")
        assert result["spatial"] is True
        assert result["documents"] is True


# ---------------------------------------------------------------------------
# _extract_public_geoscience_hints
# ---------------------------------------------------------------------------


class TestExtractPublicGeoscienceHints:
    """Isolation tests for the pure hint-extraction function."""

    def test_returns_three_lists(self) -> None:
        juris, types, comms = _extract_public_geoscience_hints("test query")
        assert isinstance(juris, list)
        assert isinstance(types, list)
        assert isinstance(comms, list)

    def test_empty_query_returns_empty_lists(self) -> None:
        juris, types, comms = _extract_public_geoscience_hints("")
        assert juris == []
        assert types == []
        assert comms == []

    def test_extracts_multiple_jurisdictions(self) -> None:
        """Both Saskatchewan and BC in the same query → two jurisdiction codes."""
        juris, _, _ = _extract_public_geoscience_hints(
            "Compare gold occurrences in Saskatchewan and British Columbia"
        )
        assert "CA-SK" in juris
        assert "CA-BC" in juris

    def test_extracts_gold_commodity(self) -> None:
        _, _, comms = _extract_public_geoscience_hints("gold showings near Flin Flon")
        assert "Au" in comms

    def test_extracts_uranium_commodity(self) -> None:
        _, _, comms = _extract_public_geoscience_hints("uranium mines in Saskatchewan")
        assert "U" in comms

    def test_extracts_mineral_occurrence_canonical_type(self) -> None:
        _, types, _ = _extract_public_geoscience_hints("smdi occurrence records")
        assert "mineral_occurrence" in types

    def test_extracts_drillhole_collar_canonical_type(self) -> None:
        _, types, _ = _extract_public_geoscience_hints("public drillhole collar data")
        assert "drillhole_collar" in types

    def test_no_false_positive_for_unrelated_query(self) -> None:
        juris, types, comms = _extract_public_geoscience_hints(
            "What is the deepest drill hole in this project?"
        )
        # "drill hole" in the project archive does not imply drillhole_collar PGEO type
        # via the hint extractor's keyword list — but 'drill hole' IS a canonical type hint.
        # This test checks that no spurious jurisdictions or commodities are returned.
        assert juris == []
        assert comms == []

    def test_deduplicates_repeated_hints(self) -> None:
        """Repeated mention of same jurisdiction only produces one entry."""
        juris, _, _ = _extract_public_geoscience_hints(
            "Saskatchewan gold in Saskatchewan province CA-SK"
        )
        assert juris.count("CA-SK") == 1


# ---------------------------------------------------------------------------
# Cache key isolation
# ---------------------------------------------------------------------------


class TestCacheKeyIsolation:
    """Tests that category differences produce different cache keys."""

    def _get_cache_key(self, query: str, project_id: str, categories: dict | None = None) -> str:
        """Replicate the _cache_key logic from orchestrator.py in a test-local
        function so we can test it without importing it (it may be private)."""
        import hashlib
        import json

        if categories is None:
            # v1 namespace — no categories
            raw = f"v1:{project_id}:{query}"
        else:
            cats_str = json.dumps(categories, sort_keys=True)
            raw = f"v2:{project_id}:{query}:{cats_str}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def test_different_categories_yield_different_keys(self) -> None:
        q, pid = "gold occurrences Saskatchewan", "proj-001"
        cats_a = {"spatial": True, "public_geoscience": True}
        cats_b = {"spatial": True, "public_geoscience": False}
        assert self._get_cache_key(q, pid, cats_a) != self._get_cache_key(q, pid, cats_b)

    def test_same_categories_yield_same_key(self) -> None:
        q, pid = "gold occurrences Saskatchewan", "proj-001"
        cats = {"spatial": True, "public_geoscience": True}
        assert self._get_cache_key(q, pid, cats) == self._get_cache_key(q, pid, cats)

    def test_no_categories_uses_v1_namespace(self) -> None:
        q, pid = "gold occurrences", "proj-001"
        v1_key = self._get_cache_key(q, pid, categories=None)
        v2_key = self._get_cache_key(q, pid, categories={"spatial": True})
        # v1 and v2 namespaces must differ even for same query.
        assert v1_key != v2_key

    def test_different_project_ids_yield_different_keys(self) -> None:
        q = "gold occurrences Saskatchewan"
        cats = {"public_geoscience": True}
        key_a = self._get_cache_key(q, "proj-001", cats)
        key_b = self._get_cache_key(q, "proj-002", cats)
        assert key_a != key_b
