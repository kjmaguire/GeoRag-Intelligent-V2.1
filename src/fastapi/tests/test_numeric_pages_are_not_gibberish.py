"""A clean assay table is not garbage OCR.

`_is_gibberish_word` returned True for any token of length >= 4 containing
no letters. Eastings, northings, elevations, sample numbers and years are
exactly that, so the pages a geologist most wants — assay tables, collar
tables, coordinate listings — scored highest on the "this OCR failed"
signal.

Measured on a 20-row collar table: gibberish_word_ratio 0.714. Three of
every four tokens on a correctly-read page were called garbage.

The costly consequence is on the DEFAULT path, no calibrated thresholds
needed: `pdf_report._native_text_screen_reason` rejects a native text layer
above NATIVE_TEXT_MAX_GIBBERISH_RATIO = 0.4, so a BORN-DIGITAL assay page
had its perfect embedded text thrown away and was routed to OCR — a billed
Document Intelligence read of a rendered image, substituted for text that
was already correct.
"""

from __future__ import annotations

import pytest

from app.services.ingest.ocr_quality import (
    _WORD_RE,
    _is_gibberish_word,
    _is_numeric_token,
    calculate_ocr_quality,
    numeric_token_ratio,
)
from app.services.ingest.pdf_report import (
    NATIVE_TEXT_MAX_GIBBERISH_RATIO,
    _native_text_screen_reason,
)


def _collar_table(rows: int = 20) -> str:
    """A born-digital collar table: hole id, easting, northing, elevation."""
    body = "\n".join(
        f"DDH-22-{i:03d}  61{2000 + i:04d}  541{2000 + i:04d}  {1200 + i}"
        for i in range(1, rows + 1)
    )
    return "Hole_ID  Easting  Northing  Elevation\n" + body


ASSAY_TABLE = "\n".join(
    [
        "Hole_ID  From_m  To_m  Length_m  U3O8_pct",
        "PLS-22-08  145  148  3  2.31",
        "PLS-22-08  151  154  3  0.87",
        "PLS-22-09  203  209  6  4.02",
        "PLS-22-10  318  322  4  1.15",
    ],
)

# The shapes a failed OCR actually produces: letterless AND not numeric.
OCR_SMEAR = ["|||#", "~~^^", "()()", "@@@@", "####", "%%%%"]

REAL_NUMBERS = ["612345", "5412345", "1250", "2022", "22-041", "1,250", "145"]


class TestNumericTokensAreNotGibberish:
    @pytest.mark.parametrize("token", REAL_NUMBERS)
    def test_a_number_is_not_gibberish(self, token: str) -> None:
        assert _is_gibberish_word(token) is False

    @pytest.mark.parametrize("token", OCR_SMEAR)
    def test_a_letterless_non_number_still_is(self, token: str) -> None:
        assert _is_gibberish_word(token) is True

    def test_a_24_digit_run_is_still_gibberish(self) -> None:
        """A number that long is a smeared line, not a coordinate."""
        assert _is_gibberish_word("1" * 24) is True

    def test_repeated_characters_are_untouched_by_the_exemption(self) -> None:
        assert _is_gibberish_word("aaaaaa") is True
        assert _is_gibberish_word("111111111") is True  # 5+ repeats

    @pytest.mark.parametrize("word", ["Easting", "Hole_ID", "granodiorite"])
    def test_ordinary_words_are_unaffected(self, word: str) -> None:
        assert _is_gibberish_word(word) is False


class TestNumericTokenPredicate:
    @pytest.mark.parametrize("token", REAL_NUMBERS)
    def test_numbers_are_numeric(self, token: str) -> None:
        assert _is_numeric_token(token) is True

    @pytest.mark.parametrize("token", ["", "----", "12a", "abc", "-,-"])
    def test_a_token_with_no_digit_is_not_numeric(self, token: str) -> None:
        """Pure punctuation must not slip through the exemption."""
        assert _is_numeric_token(token) is False

    def test_the_ratio_is_reported_as_its_own_signal(self) -> None:
        words = _WORD_RE.findall(_collar_table())

        assert numeric_token_ratio(words) > 0.5
        assert numeric_token_ratio([]) == 0.0


class TestACleanTableScoresClean:
    @pytest.mark.parametrize(
        ("name", "text"),
        [("collar", _collar_table()), ("assay", ASSAY_TABLE)],
    )
    def test_gibberish_ratio_is_under_the_native_text_cap(
        self, name: str, text: str,
    ) -> None:
        signals = calculate_ocr_quality(text, [], detected_region_count=0)

        assert signals.gibberish_word_ratio <= NATIVE_TEXT_MAX_GIBBERISH_RATIO, name

    @pytest.mark.parametrize(
        ("name", "text"),
        [("collar", _collar_table()), ("assay", ASSAY_TABLE)],
    )
    def test_a_born_digital_table_keeps_its_text_layer(
        self, name: str, text: str,
    ) -> None:
        """The regression that cost money.

        Returning a reason here routes the page to OCR, discarding an
        already-correct embedded text layer and paying for a Document
        Intelligence read of a rendered image of it.
        """
        assert _native_text_screen_reason(text, None) is None, name

    def test_a_genuinely_garbled_page_is_still_rejected(self) -> None:
        garbage = " ".join(OCR_SMEAR * 12)

        assert _native_text_screen_reason(garbage, None) == "gibberish_word_ratio"

    def test_a_prose_page_is_unaffected(self) -> None:
        prose = "The Athabasca Basin hosts unconformity-related uranium deposits. " * 12

        assert _native_text_screen_reason(prose, None) is None
