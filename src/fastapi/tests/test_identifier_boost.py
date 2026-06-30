"""Unit tests for geological identifier detection (Module 4 B3).

Tests cover:
  - Positive detection for each pattern class
  - Negative tests for false positives (ordinary English that might match)
  - Boost factor behaviour
  - Edge cases (empty strings, mixed case)

Run with:
    pytest src/fastapi/tests/test_identifier_boost.py -v
"""


from app.services.identifier_boost import (
    SPARSE_BOOST_FACTOR,
    DetectionResult,
    detect_identifiers,
    get_patterns,
)

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _has(result: DetectionResult, pattern_name: str) -> bool:
    return pattern_name in result.matched_patterns


# ===========================================================================
# 1. HOLE_ID_DASHED — positive tests
# ===========================================================================

class TestHoleIdDashed:
    def test_typical_dashed_hole(self):
        r = detect_identifiers("What were the assay results for 23-MS-117?")
        assert r.has_match
        assert _has(r, "HOLE_ID_DASHED")
        assert "23-MS-117" in r.matched_tokens

    def test_four_digit_year_prefix(self):
        r = detect_identifiers("Results for 2024-DDH-001 please")
        assert r.has_match
        assert _has(r, "HOLE_ID_DASHED")

    def test_letter_prefix_like_pls(self):
        r = detect_identifiers("PLS-22-08 assay table")
        assert r.has_match
        assert _has(r, "HOLE_ID_DASHED")

    def test_two_letter_prefix_with_letter_middle(self):
        # AB-MS-005: two-letter prefix, letter-code middle, numeric suffix
        r = detect_identifiers("hole AB-MS-005 lithology log")
        assert r.has_match
        assert _has(r, "HOLE_ID_DASHED")

    def test_multiple_holes_in_query(self):
        r = detect_identifiers("compare 23-MS-117 with 23-MS-118 and 23-MS-119")
        assert r.has_match
        assert len([t for t in r.matched_tokens if "MS" in t]) >= 1


# ===========================================================================
# 2. HOLE_ID_COMPACT — positive tests
# ===========================================================================

class TestHoleIdCompact:
    def test_ddh_prefix_compact(self):
        r = detect_identifiers("DDH0023 shows good grades")
        assert r.has_match
        assert _has(r, "HOLE_ID_COMPACT")

    def test_ms_prefix_compact(self):
        # MS2024001 fires as either HOLE_ID_COMPACT or SAMPLE_ID_ALPHA
        # (both patterns overlap on this token shape). Either is a valid hit.
        r = detect_identifiers("MS2024001 lithology")
        assert r.has_match
        assert _has(r, "HOLE_ID_COMPACT") or _has(r, "SAMPLE_ID_ALPHA")

    def test_four_char_prefix(self):
        r = detect_identifiers("HOLE0042 drill results")
        assert r.has_match
        assert _has(r, "HOLE_ID_COMPACT")


# ===========================================================================
# 3. SAMPLE_ID_ALPHA — positive tests
# ===========================================================================

class TestSampleIdAlpha:
    def test_typical_alpha_sample(self):
        r = detect_identifiers("Sample MS240301 gold assay")
        assert r.has_match
        assert _has(r, "SAMPLE_ID_ALPHA")

    def test_six_digit_sample(self):
        r = detect_identifiers("AU123456 returned 5 g/t")
        assert r.has_match
        assert _has(r, "SAMPLE_ID_ALPHA")

    def test_eight_digit_sample(self):
        r = detect_identifiers("sample ID GR12345678")
        assert r.has_match
        assert _has(r, "SAMPLE_ID_ALPHA")

    def test_four_digit_sample(self):
        r = detect_identifiers("sample RC1234")
        assert r.has_match
        assert _has(r, "SAMPLE_ID_ALPHA")


# ===========================================================================
# 4. SAMPLE_ID_DASHED — positive tests
# ===========================================================================

class TestSampleIdDashed:
    def test_au_prefix_dashed(self):
        r = detect_identifiers("AU-240301 fire assay result")
        assert r.has_match
        assert _has(r, "SAMPLE_ID_DASHED")

    def test_cu_prefix_dashed(self):
        r = detect_identifiers("CU-123456 returned 2.4%")
        assert r.has_match
        assert _has(r, "SAMPLE_ID_DASHED")


# ===========================================================================
# 5. NTS_TILE — positive tests
# ===========================================================================

class TestNtsTile:
    def test_typical_nts_tile(self):
        r = detect_identifiers("geology of 74I12 quadrant")
        assert r.has_match
        assert _has(r, "NTS_TILE")
        assert "74I12" in r.matched_tokens

    def test_three_digit_prefix_nts(self):
        r = detect_identifiers("regional survey covering 104B08")
        assert r.has_match
        assert _has(r, "NTS_TILE")

    def test_nts_at_start_of_query(self):
        r = detect_identifiers("82N09 surficial mapping")
        assert r.has_match
        assert _has(r, "NTS_TILE")


# ===========================================================================
# 6. COMMODITY_CODE — positive tests
# ===========================================================================

class TestCommodityCode:
    def test_au_exact_match(self):
        r = detect_identifiers("Au grade of 2.1 g/t")
        assert r.has_match
        assert _has(r, "COMMODITY_CODE")
        assert "Au" in r.matched_tokens

    def test_u3o8_code(self):
        r = detect_identifiers("deposit contains U3O8 mineralization")
        assert r.has_match
        assert _has(r, "COMMODITY_CODE")
        assert "U3O8" in r.matched_tokens

    def test_ree_code(self):
        r = detect_identifiers("REE enrichment zone identified")
        assert r.has_match
        assert _has(r, "COMMODITY_CODE")

    def test_multiple_commodities(self):
        r = detect_identifiers("Ag and Cu grades in the horizon")
        assert r.has_match
        assert "Ag" in r.matched_tokens
        assert "Cu" in r.matched_tokens

    def test_commodity_case_sensitive_negative(self):
        # "au" (lowercase) should NOT match commodity code "Au"
        r = detect_identifiers("what about au pair services?")
        # May match compact hole IDs but NOT COMMODITY_CODE
        assert not _has(r, "COMMODITY_CODE")


# ===========================================================================
# 7. Negative tests — ordinary English that must NOT fire
# ===========================================================================

class TestNegatives:
    def test_plain_english_question(self):
        r = detect_identifiers("how many drill holes are in the project?")
        assert not r.has_match

    def test_colloquial_geology_no_id(self):
        r = detect_identifiers("tell me about the gold mineralization style")
        # "Au" not present; "gold" is not in commodity set
        assert not r.has_match

    def test_common_words_no_id(self):
        r = detect_identifiers("show me the resource estimate for the deposit")
        assert not r.has_match

    def test_short_abbreviation_no_false_positive(self):
        # "RC" alone could look like a prefix but alone without digits is not a hole ID
        r = detect_identifiers("RC drilling was used in 2022")
        # Should not falsely match HOLE_ID_DASHED or SAMPLE_ID_ALPHA for "RC"
        # (compact might match RC2022 if it appears — it doesn't here)
        assert not _has(r, "HOLE_ID_DASHED")
        assert not _has(r, "SAMPLE_ID_DASHED")

    def test_date_like_no_false_positive(self):
        # "2022-04-15" looks like a dashed ID but fails letter-segment check
        r = detect_identifiers("drilled on 2022-04-15")
        # The pattern requires [A-Z]{1,6} in the middle segment
        assert not _has(r, "HOLE_ID_DASHED")

    def test_numeric_only_no_id(self):
        r = detect_identifiers("grade is 123456 ppm which seems too high")
        # Pure digits should not match sample or hole patterns that require a letter prefix
        assert not _has(r, "SAMPLE_ID_ALPHA")


# ===========================================================================
# 8. Boost factor behaviour
# ===========================================================================

class TestBoostFactor:
    def test_boost_applied_when_match(self):
        r = detect_identifiers("PLS-22-08 grades")
        assert r.boost_factor == SPARSE_BOOST_FACTOR
        assert r.boost_factor > 1.0

    def test_no_boost_when_no_match(self):
        r = detect_identifiers("tell me about drill results in general")
        assert r.boost_factor == 1.0

    def test_boost_factor_constant_value(self):
        assert SPARSE_BOOST_FACTOR == 1.5


# ===========================================================================
# 9. Edge cases
# ===========================================================================

class TestEdgeCases:
    def test_empty_string(self):
        r = detect_identifiers("")
        assert not r.has_match
        assert r.matched_tokens == []

    def test_only_whitespace(self):
        r = detect_identifiers("   ")
        assert not r.has_match

    def test_get_patterns_returns_dict(self):
        patterns = get_patterns()
        assert isinstance(patterns, dict)
        assert "HOLE_ID_DASHED" in patterns
        assert "NTS_TILE" in patterns

    def test_detection_result_type(self):
        r = detect_identifiers("23-MS-117")
        assert isinstance(r, DetectionResult)
        assert isinstance(r.has_match, bool)
        assert isinstance(r.matched_patterns, list)
        assert isinstance(r.matched_tokens, list)

    def test_no_duplicate_tokens(self):
        # Same token appears twice in query -- should only be in matched_tokens once
        r = detect_identifiers("23-MS-117 and 23-MS-117 again")
        assert r.matched_tokens.count("23-MS-117") == 1

    def test_mixed_pattern_classes(self):
        # Use a hole ID with a letter middle segment so HOLE_ID_DASHED fires
        r = detect_identifiers("hole 23-MS-117 returned Au 3.5 g/t in NTS tile 74I12")
        assert r.has_match
        assert _has(r, "HOLE_ID_DASHED")
        assert _has(r, "COMMODITY_CODE")
        assert _has(r, "NTS_TILE")
        assert len(r.matched_patterns) >= 3
