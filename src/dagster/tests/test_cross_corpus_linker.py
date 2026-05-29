"""Unit tests for the cross-corpus linker helpers
(georag_dagster/assets/gold_cross_corpus_linker.py).

All tests are pure unit tests — no DB, no Neo4j, no Dagster runtime.

Covers:
- _normalize_smdi
- _normalize_drillhole_id
- _extract_nts_tile
- _guess_filename
- _surrounding_context
- _SMDI_RE regex patterns (match / no-match)
- _GOS_DRILLHOLE_RE regex patterns (match / no-match)
- _NTS_TILE_RE regex patterns (match / no-match)
- _scan_document (zero links, one link, deduplication of repeated SMDI)
- LinkerStats empty-state assertions
- _CANONICAL_TYPE_TO_LABEL coverage for all 4 canonical types

Run with:  pytest tests/test_cross_corpus_linker.py -v
"""

from __future__ import annotations

import pytest

from georag_dagster.assets.gold_cross_corpus_linker import (
    _CANONICAL_TYPE_TO_LABEL,
    _GOS_DRILLHOLE_RE,
    _NTS_TILE_RE,
    _SMDI_RE,
    DocumentRow,
    LinkerStats,
    _extract_nts_tile,
    _guess_filename,
    _normalize_drillhole_id,
    _normalize_smdi,
    _scan_document,
    _surrounding_context,
)


# ---------------------------------------------------------------------------
# _normalize_smdi
# ---------------------------------------------------------------------------

class TestNormalizeSmdi:
    def test_strips_smdi_prefix_and_leading_zeros(self):
        assert _normalize_smdi("SMDI 0123") == "123"

    def test_strips_smdi_dash_prefix(self):
        assert _normalize_smdi("SMDI-99999") == "99999"

    def test_zero_stays_zero(self):
        assert _normalize_smdi("0") == "0"

    def test_non_digit_returns_none(self):
        assert _normalize_smdi("ABCDE") is None

    def test_none_returns_none(self):
        assert _normalize_smdi(None) is None

    def test_empty_string_returns_none(self):
        assert _normalize_smdi("") is None

    def test_integer_input(self):
        assert _normalize_smdi(123) == "123"

    def test_strips_all_leading_zeros(self):
        assert _normalize_smdi("000042") == "42"

    def test_already_normalized(self):
        assert _normalize_smdi("456") == "456"


# ---------------------------------------------------------------------------
# _normalize_drillhole_id
# ---------------------------------------------------------------------------

class TestNormalizeDrillholeId:
    def test_uppercase_underscore_form_unchanged(self):
        assert _normalize_drillhole_id("GOS_4482") == "GOS_4482"

    def test_lowercase_dash_form_normalised(self):
        assert _normalize_drillhole_id("gos-4482") == "GOS_4482"

    def test_whitespace_collapsed_to_underscore(self):
        assert _normalize_drillhole_id("GOS 4482") == "GOS_4482"

    def test_mixed_separators_normalised(self):
        assert _normalize_drillhole_id("gos  4482") == "GOS_4482"

    def test_none_returns_none(self):
        assert _normalize_drillhole_id(None) is None

    def test_empty_string_returns_none(self):
        assert _normalize_drillhole_id("") is None

    def test_extra_whitespace_stripped(self):
        result = _normalize_drillhole_id("  GOS_4482  ")
        assert result == "GOS_4482"


# ---------------------------------------------------------------------------
# _extract_nts_tile
# ---------------------------------------------------------------------------

class TestExtractNtsTile:
    def test_smad_filename_74h_with_underscores(self):
        # V1.2 fix: underscore-bounded NTS tiles now match. The standard
        # SMAD filename convention puts the tile between underscores
        # (MAOC_74H-0008_…), which the original \b-based regex missed.
        assert _extract_nts_tile("MAOC_74H-0008_Report.pdf") == "74H"

    def test_smad_filename_74h_with_spaces(self):
        assert _extract_nts_tile("MAOC 74H-0008 Report") == "74H"

    def test_smad_filename_104p_space_context(self):
        assert _extract_nts_tile("Survey 104P results") == "104P"

    def test_three_digit_sheet_hyphen_boundary(self):
        result = _extract_nts_tile("Report-104P-Mining")
        assert result == "104P"

    def test_two_digit_sheet_space_boundary(self):
        assert _extract_nts_tile("area 74G survey") == "74G"

    def test_underscore_delimited_three_digit(self):
        # V1.2 fix coverage: 3-digit + letter inside underscore boundaries.
        assert _extract_nts_tile("AF_104P_0012_Survey") == "104P"

    def test_empty_string_returns_none(self):
        assert _extract_nts_tile("") is None

    def test_no_nts_pattern_returns_none(self):
        assert _extract_nts_tile("Annual Report 2024.pdf") is None

    def test_returns_uppercase(self):
        result = _extract_nts_tile("data_74H_survey")
        assert result is not None
        assert result == result.upper()

    def test_lowercase_tile_normalised_to_uppercase(self):
        # Advisory #1: IGNORECASE captures lowercase tiles; .upper()
        # in _extract_nts_tile normalises the result.
        assert _extract_nts_tile("maoc_74h-0008_report") == "74H"
        assert _extract_nts_tile("area 104p results") == "104P"

    def test_does_not_false_match_inside_alphanumeric_token(self):
        # "104PA1" should NOT yield 104P — the trailing A1 means it's part
        # of a longer token (BCGS map-sheet style), not a standalone NTS tile.
        assert _extract_nts_tile("BCGS_104PA1_grid") is None

    def test_does_not_false_match_lowercase_prefix(self):
        # Advisory #2: lookbehind now includes lowercase letters so
        # "abc74H" doesn't produce a false match.
        assert _extract_nts_tile("abc74H something") is None
        assert _extract_nts_tile("text74hmore") is None


# ---------------------------------------------------------------------------
# _guess_filename
# ---------------------------------------------------------------------------

class TestGuessFilename:
    def test_pdf_in_title_extracted(self):
        result = _guess_filename("MAOC_74H-0008_Report.pdf")
        assert result == "MAOC_74H-0008_Report.pdf"

    def test_zip_in_title_extracted(self):
        result = _guess_filename("AF_74G_0012_data.zip")
        assert result == "AF_74G_0012_data.zip"

    def test_smad_prefix_no_extension(self):
        # Title starts with SMAD-style prefix (e.g. "MAOC_74H-") → returns full title
        result = _guess_filename("MAOC_74H-0008_Geochemical Survey")
        assert result is not None

    def test_plain_title_returns_none(self):
        result = _guess_filename("Annual Mineral Report for Saskatchewan 2023")
        assert result is None

    def test_empty_string_returns_none(self):
        assert _guess_filename("") is None


# ---------------------------------------------------------------------------
# _surrounding_context
# ---------------------------------------------------------------------------

class TestSurroundingContext:
    def test_returns_window_around_match(self):
        text = "a" * 100 + "SMDI 1234" + "b" * 100
        start = 100
        end = 109
        result = _surrounding_context(text, start, end, radius=10)
        assert "SMDI 1234" in result
        assert len(result) <= 10 + 9 + 10 + 2  # approximate: radius on each side + match

    def test_strips_newlines(self):
        text = "prefix\nSMDI 1234\nsuffix"
        m_start = text.index("SMDI")
        m_end = m_start + 9
        result = _surrounding_context(text, m_start, m_end, radius=20)
        assert "\n" not in result

    def test_text_at_start_doesnt_crash(self):
        text = "SMDI 42 is at the start"
        result = _surrounding_context(text, 0, 7, radius=60)
        assert "SMDI" in result

    def test_text_at_end_doesnt_crash(self):
        text = "text at the end SMDI 42"
        result = _surrounding_context(text, len(text) - 7, len(text), radius=60)
        assert "SMDI" in result

    def test_result_is_stripped(self):
        text = "   SMDI 1234   "
        result = _surrounding_context(text, 3, 12, radius=5)
        assert result == result.strip()


# ---------------------------------------------------------------------------
# Regex — _SMDI_RE
# ---------------------------------------------------------------------------

class TestSmdiRegex:
    @pytest.mark.parametrize("text,expected_group1", [
        # The regex is \bSMDI[-\s]?0*([0-9]{1,5})\b — the 0* strips leading
        # zeros BEFORE the capture group, so "SMDI 0123" → group(1) == "123".
        ("SMDI 0123",  "123"),
        ("SMDI-123",   "123"),
        ("SMDI0123",   "123"),
        ("smdi 99",    "99"),
        ("reference SMDI 5000 in text", "5000"),
    ])
    def test_matches(self, text, expected_group1):
        m = _SMDI_RE.search(text)
        assert m is not None, f"Pattern did not match '{text}'"
        assert m.group(1) == expected_group1

    @pytest.mark.parametrize("text", [
        "ESMDIQ 123",   # 'E' prefix guard boundary
        "SMDIs 456",    # trailing 's' guard boundary
        "noSMDI123",    # no word boundary on left
    ])
    def test_no_match(self, text):
        m = _SMDI_RE.search(text)
        assert m is None, f"Pattern should NOT match '{text}' but did"


# ---------------------------------------------------------------------------
# Regex — _GOS_DRILLHOLE_RE
# ---------------------------------------------------------------------------

class TestGosDrillholeRegex:
    @pytest.mark.parametrize("text", [
        "GOS_4482",
        "GOS 4482",
        "gos-12345",
        "refer to GOS_4482 in report",
    ])
    def test_matches(self, text):
        m = _GOS_DRILLHOLE_RE.search(text)
        assert m is not None, f"Pattern should match '{text}'"

    @pytest.mark.parametrize("text", [
        "GOS",          # no digits
        "GOSS_4482",    # extra S before _
        "MGOS_4482",    # M prefix breaks word boundary
    ])
    def test_no_match(self, text):
        m = _GOS_DRILLHOLE_RE.search(text)
        assert m is None, f"Pattern should NOT match '{text}' but did"


# ---------------------------------------------------------------------------
# Regex — _NTS_TILE_RE
# ---------------------------------------------------------------------------

class TestNtsTileRegex:
    @pytest.mark.parametrize("text", [
        "74H",
        "104P",
        "82G",
        "area 74H tile",
        "MAOC_74H-0008",      # V1.2 fix — underscore boundary
        "AF_104P_0012",        # underscore both sides
        "Survey-74H-2020",
        "(74H)",               # parens
        "74h",                 # Advisory #1 — lowercase tile via IGNORECASE
        "maoc_74h-0008",       # fully lowercased SMAD filename
        "104p",                # lowercase 3-digit sheet
    ])
    def test_matches(self, text):
        m = _NTS_TILE_RE.search(text)
        assert m is not None, f"Pattern should match '{text}'"

    @pytest.mark.parametrize("text", [
        "12345",     # all digits
        "XY",        # no digits at all
        "ABC",       # letters only
        "104PA1",    # alphanumeric continuation — would false-match
        "AB74H",     # leading uppercase letters
        "ab74H",     # Advisory #2 — leading lowercase letters
        "abc74h",    # fully lowercase prefix — lookbehind blocks
        "text74Hmore",  # embedded in continuous alpha run
    ])
    def test_no_match(self, text):
        m = _NTS_TILE_RE.search(text)
        assert m is None, f"Pattern should NOT match '{text}' but did"


# ---------------------------------------------------------------------------
# _scan_document
# ---------------------------------------------------------------------------

def _make_doc(body_text: str, report_id: str = "doc-001") -> DocumentRow:
    return DocumentRow(
        report_id=report_id,
        title="Test Document",
        filename=None,
        nts_tile=None,
        body_text=body_text,
    )


class TestScanDocument:
    def test_no_patterns_returns_empty_list(self):
        doc = _make_doc("This document has no geological identifiers.")
        links = _scan_document(doc, smdi_lookup={}, drillhole_lookup={})
        assert links == []

    def test_smdi_match_in_lookup_returns_link(self):
        doc = _make_doc("The occurrence SMDI 42 is located in the study area.")
        smdi_lookup = {"42": "uuid-mineral-occ-42"}
        links = _scan_document(doc, smdi_lookup=smdi_lookup, drillhole_lookup={})
        assert len(links) == 1
        assert links[0].canonical_type == "mineral_occurrence"
        assert links[0].entity_id == "uuid-mineral-occ-42"

    def test_smdi_not_in_lookup_returns_empty(self):
        doc = _make_doc("Reference to SMDI 9999 not in our database.")
        links = _scan_document(doc, smdi_lookup={}, drillhole_lookup={})
        assert links == []

    def test_drillhole_match_returns_link(self):
        doc = _make_doc("See drillhole GOS_4482 for details.")
        dh_lookup = {"GOS_4482": "uuid-dh-4482"}
        links = _scan_document(doc, smdi_lookup={}, drillhole_lookup=dh_lookup)
        assert len(links) == 1
        assert links[0].canonical_type == "drillhole_collar"
        assert links[0].entity_id == "uuid-dh-4482"

    def test_repeated_smdi_in_same_doc_deduplicates(self):
        """The same SMDI appearing twice in one document → exactly one link."""
        doc = _make_doc(
            "First mention: SMDI 42. Some other text. Second mention: SMDI 42 again."
        )
        smdi_lookup = {"42": "uuid-mineral-occ-42"}
        links = _scan_document(doc, smdi_lookup=smdi_lookup, drillhole_lookup={})
        assert len(links) == 1

    def test_multiple_different_smdis_each_produce_link(self):
        doc = _make_doc("See SMDI 10 and SMDI 20 for more.")
        smdi_lookup = {"10": "uuid-10", "20": "uuid-20"}
        links = _scan_document(doc, smdi_lookup=smdi_lookup, drillhole_lookup={})
        assert len(links) == 2
        entity_ids = {l.entity_id for l in links}
        assert entity_ids == {"uuid-10", "uuid-20"}

    def test_link_confidence_is_deterministic_value(self):
        doc = _make_doc("SMDI 5 is here.")
        links = _scan_document(doc, smdi_lookup={"5": "uuid-5"}, drillhole_lookup={})
        assert links[0].confidence == 0.95

    def test_link_signals_contains_smdi_id_match(self):
        doc = _make_doc("SMDI 5 is here.")
        links = _scan_document(doc, smdi_lookup={"5": "uuid-5"}, drillhole_lookup={})
        assert "smdi_id_match" in links[0].signals

    def test_link_signals_contains_drillhole_id_match(self):
        doc = _make_doc("See GOS_100 for collar data.")
        links = _scan_document(
            doc, smdi_lookup={}, drillhole_lookup={"GOS_100": "uuid-dh-100"}
        )
        assert "drillhole_id_match" in links[0].signals

    def test_mixed_smdi_and_drillhole_links(self):
        doc = _make_doc("SMDI 10 is near drillhole GOS_999.")
        smdi_lookup = {"10": "uuid-smdi-10"}
        dh_lookup = {"GOS_999": "uuid-dh-999"}
        links = _scan_document(doc, smdi_lookup=smdi_lookup, drillhole_lookup=dh_lookup)
        assert len(links) == 2
        types = {l.canonical_type for l in links}
        assert types == {"mineral_occurrence", "drillhole_collar"}


# ---------------------------------------------------------------------------
# LinkerStats empty-state assertions
# ---------------------------------------------------------------------------

class TestLinkerStatsEmptyState:
    def test_default_proposed_links_is_zero(self):
        stats = LinkerStats()
        assert stats.proposed_links == 0

    def test_default_new_links_inserted_is_zero(self):
        stats = LinkerStats()
        assert stats.new_links_inserted == 0

    def test_empty_scan_keeps_stats_at_zero(self):
        stats = LinkerStats()
        doc = _make_doc("No identifiers in this document at all.")
        links = _scan_document(doc, smdi_lookup={}, drillhole_lookup={})
        stats.proposed_links += len(links)
        assert stats.proposed_links == 0
        assert stats.new_links_inserted == 0

    def test_empty_scaffolding_flag_expression(self):
        """Reproduces the logic in _stats_to_metadata for empty_scaffolding."""
        stats = LinkerStats()
        assert (stats.proposed_links == 0) is True


# ---------------------------------------------------------------------------
# _CANONICAL_TYPE_TO_LABEL exhaustiveness
# ---------------------------------------------------------------------------

class TestCanonicalTypeToLabel:
    EXPECTED_CANONICAL_TYPES = {
        "mine",
        "mineral_occurrence",
        "drillhole_collar",
        "resource_potential_zone",
    }

    def test_all_four_canonical_types_present(self):
        assert set(_CANONICAL_TYPE_TO_LABEL.keys()) == self.EXPECTED_CANONICAL_TYPES

    def test_mine_maps_to_Mine(self):
        assert _CANONICAL_TYPE_TO_LABEL["mine"] == "Mine"

    def test_mineral_occurrence_maps_to_MineralOccurrence(self):
        assert _CANONICAL_TYPE_TO_LABEL["mineral_occurrence"] == "MineralOccurrence"

    def test_drillhole_collar_maps_to_Drillhole(self):
        assert _CANONICAL_TYPE_TO_LABEL["drillhole_collar"] == "Drillhole"

    def test_resource_potential_zone_maps_to_ResourcePotentialZone(self):
        assert _CANONICAL_TYPE_TO_LABEL["resource_potential_zone"] == "ResourcePotentialZone"

    def test_no_extra_keys(self):
        assert len(_CANONICAL_TYPE_TO_LABEL) == 4
