"""An unrecognised OPTIONAL value blanks the field; it must not drop the row.

WHY THIS FILE EXISTS
    csv_lithology / csv_survey / csv_sample each carry a fixed vocabulary for
    a few optional descriptive columns. A value outside it used to reject the
    whole row: one "porphyritic" in the Texture column threw away the interval
    and, with it, the hole's strip log - while hole, depths and lithology code
    were fine. Kyle approved "keep the row, blank the field, say so"
    (2026-09-29). Required fields still reject.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from georag_geoparsers import (
    parse_csv_lithology,
    parse_csv_samples,
    parse_csv_surveys,
)
from georag_geoparsers._optional_enum import BlankedValues, canonical_choice

_LITH = (
    "HoleID,From,To,Lithology,Texture,Hardness,Weathering\n"
    "D1,0,5,Andesite,Fine,hard,Fresh\n"          # case-only difference: canonicalised
    "D1,5,9,Tuff,porphyritic,Soft,unweathered\n"  # texture + weathering blanked
    "D1,9,12,Andesite,Coarse,mush,Fresh\n"       # hardness blanked
)


def _warning(result, code="optional_values_blanked"):
    found = [w for w in result.warnings if w.get("code") == code]
    return found[0] if found else None


class TestLithology:
    def test_rows_are_kept_and_only_the_bad_field_is_null(self) -> None:
        result = parse_csv_lithology(io.StringIO(_LITH))

        assert result.valid_rows == 3
        assert result.skipped_rows == 0
        first, second, third = result.records
        assert (first["grain_size"], first["hardness"], first["weathering"]) == (
            "Fine", "Hard", "Fresh",
        )
        assert (second["grain_size"], second["hardness"], second["weathering"]) == (
            None, "Soft", None,
        )
        assert (third["grain_size"], third["hardness"], third["weathering"]) == (
            "Coarse", None, "Fresh",
        )
        # The required fields of the blanked rows are intact.
        assert (second["hole_id"], second["from_depth"], second["to_depth"],
                second["lithology_code"]) == ("D1", 5.0, 9.0, "Tuff")

    def test_the_blanked_value_is_kept_in_the_description(self) -> None:
        """Blanked from the vocabulary field, not from the record: the raw
        word lands in lithology_description so nothing the geologist wrote
        is lost."""
        result = parse_csv_lithology(io.StringIO(
            "HoleID,From,To,Lithology,Description,Texture\n"
            "D1,0,5,Tuff,welded,porphyritic\n"
            "D1,5,9,Tuff,,glassy\n"
            "D1,9,12,Tuff,crystal tuff,Fine\n"
        ))

        first, second, third = result.records
        assert first["lithology_description"] == "welded [grain_size: porphyritic]"
        assert second["lithology_description"] == "[grain_size: glassy]"
        assert third["lithology_description"] == "crystal tuff"  # nothing blanked

    def test_the_warning_counts_per_field_with_examples(self) -> None:
        result = parse_csv_lithology(io.StringIO(_LITH))

        warning = _warning(result)
        assert warning is not None
        assert warning["fields"] == {
            "grain_size": {"count": 1, "examples": ["porphyritic"]},
            "hardness": {"count": 1, "examples": ["mush"]},
            "weathering": {"count": 1, "examples": ["unweathered"]},
        }
        # message AND detail: the Ingestion Runs page renders `detail`.
        assert warning["message"] and warning["detail"]
        assert "porphyritic" in warning["detail"]
        assert "rows were kept" in warning["message"]

    def test_a_clean_file_has_no_warning(self) -> None:
        result = parse_csv_lithology(io.StringIO(
            "HoleID,From,To,Lithology,Texture\nD1,0,5,Andesite,Fine\n"
        ))
        assert _warning(result) is None
        assert result.valid_rows == 1

    def test_required_fields_still_reject_the_row(self) -> None:
        result = parse_csv_lithology(io.StringIO(
            "HoleID,From,To,Lithology,Texture\n"
            "D1,0,5,,porphyritic\n"      # no lithology code: rejected
            "D1,7,5,Tuff,Fine\n"          # inverted interval: rejected
            ",0,5,Tuff,Fine\n"            # no hole: rejected
            "D1,0,5,Tuff,Fine\n"          # kept
        ))
        assert result.valid_rows == 1
        assert result.skipped_rows == 3
        assert sorted(d["code"] for d in result.skipped_details) == [
            "depth_order_invalid", "missing_required", "missing_required",
        ]
        # The rejected row's bad Texture is not counted as a blanked value:
        # only rows that LANDED are reported as kept-with-a-blank.
        assert _warning(result) is None or "grain_size" not in _warning(result)["fields"]

    def test_examples_are_bounded(self) -> None:
        rows = "".join(f"D1,{i},{i + 1},Tuff,texture{i}\n" for i in range(10))
        result = parse_csv_lithology(io.StringIO(
            "HoleID,From,To,Lithology,Texture\n" + rows
        ))
        info = _warning(result)["fields"]["grain_size"]
        assert info["count"] == 10
        assert len(info["examples"]) == 3


class TestSurvey:
    _CSV = (
        "HoleID,Depth,Azimuth,Dip,Method\n"
        "D1,0,45,-60,Reflex\n"
        "D1,30,46,-61,Multishot\n"        # not in the list: blanked, station kept
        "D1,60,47,-62,gyro\n"              # case difference: canonicalised
    )

    def test_station_is_kept_with_a_null_method(self) -> None:
        result = parse_csv_surveys(io.StringIO(self._CSV))

        assert result.valid_rows == 3 and result.skipped_rows == 0
        assert [r["survey_method"] for r in result.records] == [
            "Reflex", None, "Gyro",
        ]
        warning = _warning(result)
        assert warning["fields"] == {
            "survey_method": {"count": 1, "examples": ["Multishot"]},
        }

    def test_required_survey_fields_still_reject(self) -> None:
        result = parse_csv_surveys(io.StringIO(
            "HoleID,Depth,Azimuth,Dip,Method\n"
            "D1,0,400,-60,Multishot\n"     # azimuth out of range: rejected
            "D1,30,45,-60,Multishot\n"      # kept, method blanked
        ))
        assert result.valid_rows == 1
        assert result.skipped_rows == 1
        assert result.skipped_details[0]["code"] == "range_check_failed"


class TestSample:
    _CSV = (
        "HoleID,SampleID,From,To,SampleType,QAQC,Au_ppm\n"
        "D1,S1,0,1,Core,Primary,0.5\n"
        "D1,S2,1,2,Core,CRM-1,0.6\n"       # unreadable qaqc: blanked, NOT 'Primary'
        "D1,S3,2,3,Core,duplicate,0.7\n"   # case difference: canonicalised
    )

    def test_unreadable_qaqc_is_null_not_primary(self) -> None:
        result = parse_csv_samples(io.StringIO(self._CSV))

        assert result.valid_rows == 3 and result.skipped_rows == 0
        assert [r["qaqc_type"] for r in result.records] == [
            "Primary", None, "Duplicate",
        ]
        assert _warning(result)["fields"] == {
            "qaqc_type": {"count": 1, "examples": ["CRM-1"]},
        }

    def test_unknown_sample_type_is_blanked_not_rejected(self) -> None:
        """Kyle, 2026-09-29 (ING-4): keep the row - and its assays - blank the
        type, say so. It used to reject the whole row."""
        result = parse_csv_samples(io.StringIO(
            "HoleID,SampleID,From,To,SampleType,Au_ppm\n"
            "D1,S1,0,1,Mystery,0.5\n"
            "D1,S2,1,2,Core,0.6\n"
        ))
        assert result.valid_rows == 2 and result.skipped_rows == 0
        assert [r["sample_type"] for r in result.records] == [None, "Core"]
        assert result.records[0]["commodity_assays"] == {"Au_ppm": 0.5}
        assert _warning(result)["fields"] == {
            "sample_type": {"count": 1, "examples": ["Mystery"]},
        }


class TestHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("very  coarse", "Very Coarse"), ("HARD", "Hard"), (" fine ", "Fine"),
         ("porphyritic", None), ("", None), (None, None)],
    )
    def test_canonical_choice_is_spelling_only(self, raw, expected) -> None:
        valid = frozenset({"Fine", "Hard", "Very Coarse"})
        assert canonical_choice(raw, valid) == expected

    def test_nothing_recorded_means_no_warning(self) -> None:
        assert BlankedValues().as_warning(parser="x") is None


class TestWorkbookSheetsForwardTheWarning:
    """A .xlsx sheet goes through the same CSV parser, but ExcelParseResult
    used to drop the parser's warnings - so a workbook would have been the
    quiet way to lose the value."""

    def test_xlsx_lithology_sheet_carries_the_warning(self, tmp_path: Path) -> None:
        openpyxl = pytest.importorskip("openpyxl")
        from georag_geoparsers.xlsx_parser import parse_xlsx_sheet

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Litho"
        for row in [
            ["HoleID", "From", "To", "Lithology", "Texture"],
            ["D1", 0, 5, "Andesite", "Fine"],
            ["D1", 5, 9, "Tuff", "porphyritic"],
        ]:
            ws.append(row)
        path = tmp_path / "log.xlsx"
        wb.save(path)

        try:
            result = parse_xlsx_sheet(str(path), "Litho", "lithology")
        except Exception as exc:  # pragma: no cover - reader backend missing
            pytest.skip(f"xlsx reader unavailable: {exc}")

        assert result.valid_rows == 2
        assert _warning(result)["fields"]["grain_size"]["count"] == 1

    def test_forwarding_without_an_excel_backend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same claim with the workbook read stubbed, so it runs where
        fastexcel is not installed."""
        import polars as pl

        from georag_geoparsers import xlsx_parser

        frame = pl.DataFrame({
            "HoleID": ["D1", "D1"], "From": ["0", "5"], "To": ["5", "9"],
            "Lithology": ["Andesite", "Tuff"], "Texture": ["Fine", "porphyritic"],
        })
        monkeypatch.setattr(xlsx_parser.pl, "read_excel", lambda *a, **k: frame)
        path = tmp_path / "log.xlsx"
        path.write_bytes(b"x")   # only its extension and hash are used

        result = xlsx_parser.parse_xlsx_sheet(str(path), "Litho", "lithology")

        assert result.valid_rows == 2
        assert _warning(result)["fields"]["grain_size"]["examples"] == ["porphyritic"]
        # Transport-level CSV notes are NOT forwarded.
        assert all(w["code"] == "optional_values_blanked" for w in result.warnings)
