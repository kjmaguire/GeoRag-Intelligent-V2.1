"""The long-format assay pivot keeps every element, however late it first appears.

WHY THIS FILE EXISTS (audit finding 8)
    ``_pivot_long_to_wide`` assembled one dict per sample group and handed the
    list to ``pl.DataFrame``, which infers its columns from the FIRST 100 dicts
    only. An element first seen in the 101st group - Au for a hundred samples,
    then the first Mo - had its whole column dropped: no error, no warning,
    and the sample records simply carried no such assay.
"""

from __future__ import annotations

import io

import polars as pl

from georag_geoparsers.csv_sample import parse_csv_samples


def _long_file(n_samples: int, late_element_from: int) -> str:
    lines = ["HoleID,From,To,SampleID,Element,Value,Unit"]
    for i in range(n_samples):
        lines.append(f"DH-1,{i},{i + 1},S{i:04d},Au,{0.1 + i / 1000:.3f},ppm")
        if i >= late_element_from:
            lines.append(f"DH-1,{i},{i + 1},S{i:04d},Mo,{5 + i},ppm")
    return "\n".join(lines) + "\n"


class TestAnElementFirstSeenAfterTheFirstHundredGroups:
    def test_its_column_survives_and_carries_the_values(self) -> None:
        result = parse_csv_samples(io.StringIO(_long_file(150, late_element_from=120)))

        assert "Mo_ppm" in result.assay_columns
        assert "Au_ppm" in result.assay_columns
        assert result.valid_rows == 150

        by_sample = {r["sample_id"]: r["commodity_assays"] for r in result.records}
        assert "Mo_ppm" not in by_sample["S0000"]
        assert by_sample["S0119"].get("Mo_ppm") is None
        assert by_sample["S0120"]["Mo_ppm"] == 125.0
        assert by_sample["S0149"]["Mo_ppm"] == 154.0
        # Every record still has its gold.
        assert all("Au_ppm" in assays for assays in by_sample.values())

    def test_the_pivot_keeps_all_hundred_and_fifty_columns(self) -> None:
        """The bound is the schema inference, not one element: with a new
        element in every group, columns 101-150 are all late. Called on the
        pivot directly - there are not 150 assayable element symbols to
        build this from a real file."""
        from georag_geoparsers.csv_sample import _pivot_long_to_wide

        elements = [f"E{i:03d}" for i in range(150)]
        long = pl.DataFrame({
            "HoleID": ["DH-1"] * 150,
            "From": [str(i) for i in range(150)],
            "To": [str(i + 1) for i in range(150)],
            "Element": elements,
            "Value": [str(i + 1) for i in range(150)],
            "Unit": ["ppm"] * 150,
        })
        result = _pivot_long_to_wide(long, long.columns, [])

        assert result is not None
        wide_df, assay_col_names, _flags, _ambiguity = result
        expected = [f"{e}_ppm" for e in elements]
        assert assay_col_names == expected
        assert set(expected) <= set(wide_df.columns)
        assert wide_df.height == 150
        # Each column holds its one value, and only that row has it.
        for i, column in enumerate(expected):
            values = wide_df[column].to_list()
            assert values[i] == float(i + 1)
            assert sum(v is not None for v in values) == 1

    def test_a_short_file_is_unchanged(self) -> None:
        result = parse_csv_samples(io.StringIO(_long_file(3, late_element_from=1)))

        assert {r["sample_id"]: r["commodity_assays"] for r in result.records}["S0002"] == {
            "Au_ppm": 0.102, "Mo_ppm": 7.0,
        }
