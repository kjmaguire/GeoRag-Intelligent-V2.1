"""A document's name in the UI must be something the uploader recognises.

Written against a real report: files ingested from a customer delivery showed
up under Reports with names like ``<figure>`` and single letters, because the
title was read from inside the PDF with no sanity check and the filename was
never consulted. The parsed title is a HINT about a document; the filename is
its identity.
"""

import pytest

from app.services.ingest.pdf_report import _derive_title, _looks_like_a_title


class TestLooksLikeATitle:
    @pytest.mark.parametrize("junk", [
        "<figure>",           # the one that shipped
        "<table>",
        "<page_header>",
        "[image]",
        "A",                  # a dropped cap
        "3",
        "Page 12",
        "iv",                 # front-matter page number
        "Figure 4",
        "Table 1",
        "Contents",
        "   ",
        "",
        "...",                # punctuation only: no word character
        "---",
    ])
    def test_layout_artefacts_are_not_titles(self, junk):
        assert _looks_like_a_title(junk) is False

    @pytest.mark.parametrize("real", [
        "Technical Report on the Shumagin Gold Project",
        "NI 43-101 Unga Island",
        "2016 Surface Geochemistry",
        "Apollo-Sitka Underground Workings",
    ])
    def test_real_titles_survive(self, real):
        assert _looks_like_a_title(real) is True

    def test_a_short_but_genuine_name_is_sacrificed_deliberately(self):
        # "Unga" is a real place and a plausible title, but nothing separates
        # it from a stray token by shape alone. Rejecting it costs a fallback
        # to the filename -- recognisable and recoverable. Accepting the class
        # it belongs to is what let "<figure>" through.
        assert _looks_like_a_title("Unga") is False


class TestDeriveTitle:
    def test_pdf_metadata_title_wins_when_it_is_real(self):
        assert _derive_title(
            "Technical Report on the Shumagin Project", "body text", "/x/20260824_120000_report.pdf",
        ) == "Technical Report on the Shumagin Project"

    def test_first_line_is_used_when_metadata_is_empty(self):
        assert _derive_title(
            "", "Geological Mapping of Unga Island\nmore body", "/x/a.pdf",
        ) == "Geological Mapping of Unga Island"

    def test_figure_metadata_falls_through_to_the_filename(self):
        # The exact reported bug: <figure> reached the Reports list as the
        # document's name.
        assert _derive_title(
            "<figure>", "<figure>\n<table>", "/x/Shumagin DDH44-59 logs&assays.pdf",
        ) == "Shumagin DDH44-59 logs&assays"

    def test_single_letter_first_line_falls_through_to_the_filename(self):
        assert _derive_title("", "A\nB\nC", "/x/TR005-Geology.pdf") == "TR005-Geology"

    def test_filename_fallback_drops_only_the_extension(self):
        # The stem, not the whole name: the extension is noise in a heading.
        # Dots inside the name must survive.
        assert _derive_title("", "", "/x/C 5 - Diamond Drill Holes 21 - 43.pdf") == (
            "C 5 - Diamond Drill Holes 21 - 43"
        )

    def test_a_pathless_nameless_file_still_yields_something(self):
        # silver.reports.title is NOT NULL, so this may never return "".
        assert _derive_title("", "", "") == "(untitled)"

    def test_empty_body_does_not_raise_on_the_first_line_lookup(self):
        # full_text[:200].splitlines()[0] is an IndexError on "".
        assert _derive_title("", "", "/x/a.pdf") == "a"
