"""A table that is not a drill table must still land as data.

Covers the three shapes the RedStar delivery produced, all of which
previously reached the geologist as prose only:

* a 66-column assay certificate uploaded under the `collars` category,
* an IP station list that scored 3/4 on collar because X/Y/Z alias to
  easting/northing/elevation,
* a workbook whose sheets match nothing at all.
"""

import csv

import pytest
from georag_geoparsers._sheet_classifier import classify_sheet_type

from app.hatchet_workflows.ingest_tabular import (
    _read_delimited_rows,
    _wrote_nothing_warning,
)


class TestIdentityField:
    """`hole_id` is what makes a drill table a drill table."""

    def test_ip_stations_no_longer_classify_as_collar(self):
        # Grids_Name/LineNumber/X/Y/Z -- X/Y/Z alias to easting/northing/
        # elevation, which used to clear the 0.75 coverage threshold.
        assert classify_sheet_type(
            ["Grids_Name", "LineNumber", "X", "Y", "Z"],
        ) == ("unknown", 0.0)

    def test_assay_certificate_does_not_classify(self):
        assert classify_sheet_type(
            ["Sample #", "Au_ppm_final", "Ag_ppm", "Cu_ppm", "As_ppm"],
        ) == ("unknown", 0.0)

    @pytest.mark.parametrize(("headers", "expected"), [
        (["hole_id", "easting", "northing", "elevation"], "collar"),
        (["hole_id", "depth", "azimuth", "dip"], "survey"),
        (["hole_id", "from_depth", "to_depth", "lithology_code"], "lithology"),
        (["hole_id", "from_depth", "to_depth", "sample_type"], "sample"),
    ])
    def test_real_drill_sheets_still_classify(self, headers, expected):
        sheet_type, confidence = classify_sheet_type(headers)
        assert (sheet_type, confidence) == (expected, 1.0)

    def test_partial_collar_still_classifies(self):
        # Three of four required, and the missing one is not the identity.
        assert classify_sheet_type(
            ["hole_id", "easting", "northing"],
        ) == ("collar", 0.75)

    def test_discriminator_cannot_override_a_missing_hole_id(self):
        # `sample_type` is a hard discriminator, but a sample with no hole
        # cannot be resolved to a collar and so cannot be written.
        assert classify_sheet_type(
            ["sample_type", "from_depth", "to_depth"],
        ) == ("unknown", 0.0)


class TestDelimitedRows:
    def test_reads_rows_as_dicts(self, tmp_path):
        p = tmp_path / "assay.csv"
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Sample #", "Au_ppm", "Ag_ppm"])
            w.writerow(["P410301", "0.016", "2.63"])
            w.writerow(["P410302", "0.008", "1.32"])

        rows = _read_delimited_rows(str(p))

        assert len(rows) == 2
        assert rows[0]["Sample #"] == "P410301"
        assert rows[1]["Au_ppm"] == "0.008"

    def test_honours_a_semicolon_delimiter(self, tmp_path):
        p = tmp_path / "euro.csv"
        p.write_text(
            "Sample;Au_ppm\nP1;0,016\n", encoding="utf-8", newline="",
        )

        rows = _read_delimited_rows(str(p))

        assert rows == [{"Sample": "P1", "Au_ppm": "0,016"}]


class TestWroteNothingWording:
    """The message must not claim a layout match that never happened."""

    def test_category_forced_does_not_claim_a_layout_match(self):
        w = _wrote_nothing_warning(
            label="FA16099231_edit.csv",
            classified_as="collar",
            reason="missing required column mapping(s): 'hole_id'",
            from_category=True,
        )
        assert "matched the collar layout" not in w["detail"]
        assert "uploaded to the collar category" in w["detail"]
        # The actionable half: change the category, not the column names.
        assert "re-upload it under the category that matches" in w["detail"]

    def test_classifier_match_still_says_so(self):
        w = _wrote_nothing_warning(
            label="export_UTM",
            classified_as="collar",
            reason="missing required column mapping(s): 'hole_id'",
            from_category=False,
        )
        assert "matched the collar layout" in w["detail"]
        assert "rename its columns" in w["detail"]

    def test_always_carries_a_detail(self):
        # The Ingestion Runs page renders `detail` and falls back to `code`;
        # a warning with neither shows a bare token.
        for forced in (True, False):
            w = _wrote_nothing_warning(
                label="x", classified_as="sample", reason=None,
                from_category=forced,
            )
            assert w["detail"] and w["message"]
            assert w["code"] == "classified_but_nothing_written"
