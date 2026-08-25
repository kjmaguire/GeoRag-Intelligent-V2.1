"""Tests for UBC-GIF DCIP2D inversion output parsing.

Every number asserted against the L3750N export below was measured by hand
from the delivered files before the parser was written. They are regression
guards, not illustrations: if a refactor changes one of them, the refactor
changed what the pipeline believes about a real survey.

Three claims in particular are load-bearing and are asserted rather than
commented, because each one fails SILENTLY if it is wrong:

  * the observed-data columns are four electrode chainages and a measurement,
    not x/y/z — ``test_c1_equals_c2_in_every_row`` and the chainage bounds;
  * the model storage order is row-major, surface first — ``test_air_mask_is_a
    _topographic_surface_row_major`` versus its column-major counterpart;
  * the dc and ip inversions share one mesh — ``test_air_mask_is_identical
    _across_both_model_families``.

The synthetic tests at the bottom cover malformed input, which the delivery
does not contain and which therefore cannot be measured from it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from georag_geoparsers.dcip2d_parser import (
    AIR_FLAG,
    DATA_COLUMNS,
    INP_UNSET,
    read_dcip2d_data,
    read_dcip2d_model,
    read_inp,
)

# The delivered DCIP2D run: Centennial, IP survey of 2005-06-19, line 3750 N.
EXPORT_DIR = Path(
    "C:/Users/GeoRAG/Desktop/RedStar/Centennial/Geophysics/IP/June 19/L3750N/export"
)

#: Applied to the classes that read the delivery, NOT to the whole module —
#: the malformed-input tests are pure and must keep running on a machine that
#: does not have the RedStar hand-off mounted.
needs_export = pytest.mark.skipif(
    not EXPORT_DIR.is_dir(),
    reason=f"RedStar DCIP2D export not present at {EXPORT_DIR}",
)

DC_MODELS = ("dcinv2d.011", "dcinv2d.016", "dcinv2d.030")
IP_MODELS = ("ipinv2d.001", "ipinv2d.011", "ipinv2d.016", "ipinv2d.chg")

#: The three geometry splits the survey never populated. Header written,
#: zero rows — a normal export, not a broken file.
STUBS = (
    "CEN_L3750_Mx_XYZ.rdtmm",
    "CEN_L3750_Mx_XYZ.rdtmp",
    "CEN_L3750_Vp_XYZ.rdtmm",
)


def _air_run_lengths_from_top(mask: np.ndarray) -> list[int] | None:
    """Air-cell count per column, or None if any column's air is not a top run.

    Topography can only put air ABOVE rock. A mask that puts an air cell under
    a rock cell is not a topographic surface, which is how the wrong reshape
    order is detected without eyeballing a picture.
    """
    counts: list[int] = []
    for column in mask.T:
        depth = int(column.sum())
        if not column[:depth].all():
            return None
        counts.append(depth)
    return counts


# ---------------------------------------------------------------------------
# Observed data — CEN_L3750_Vp_XYZ.rdtmd (the one split that carries readings)
# ---------------------------------------------------------------------------

@needs_export
class TestObservedData:
    @pytest.fixture(scope="class")
    def vp(self):
        return read_dcip2d_data(EXPORT_DIR / "CEN_L3750_Vp_XYZ.rdtmd")

    def test_header_is_title_then_array_type(self, vp):
        assert vp.title == (
            "Normalized Potential - Line 3750 N Current Line = Dipole Line & C1 > P1"
        )
        assert vp.array_type == "Pole-Dipole"

    def test_row_count(self, vp):
        assert len(vp.records) == 96

    def test_every_row_has_the_five_electrode_and_value_fields(self, vp):
        assert DATA_COLUMNS == ("C1", "C2", "P1", "P2", "value")
        assert {len(record) for record in vp.records} == {5}

    def test_first_and_last_records_are_exact(self, vp):
        # Fixed-width text parsed to doubles; these are exact, not approximate.
        assert vp.records[0] == (5494.88, 5494.88, 5442.79, 5404.70, 0.4126582)
        assert vp.records[-1] == (4596.62, 4596.62, 4547.55, 4500.00, 0.5693103)

    def test_c1_equals_c2_in_every_row(self, vp):
        """Pole-dipole: C2 is at infinity, so the writer repeats C1.

        This is also the tell that the file is not x/y/z — a real coordinate
        export does not have easting identical to northing in all 96 rows.
        """
        assert all(record[0] == record[1] for record in vp.records)

    def test_normalized_potential_range(self, vp):
        values = [record[4] for record in vp.records]
        assert min(values) == 0.0106974
        assert max(values) == 1.0481928

    def test_chainage_bounds_are_one_dimensional(self, vp):
        """All four electrode columns live on ONE 1-D chainage, 4500..5494.88 m.

        A second spatial axis would not share bounds with the first.
        """
        electrodes = [value for record in vp.records for value in record[:4]]
        assert min(electrodes) == 4500.00
        assert max(electrodes) == 5494.88

    def test_twenty_current_poles_up_to_six_n_levels(self, vp):
        per_pole: dict[float, int] = {}
        for c1, _c2, _p1, _p2, _value in vp.records:
            per_pole[c1] = per_pole.get(c1, 0) + 1
        assert len(per_pole) == 20
        assert max(per_pole.values()) == 6


@needs_export
@pytest.mark.parametrize("name", STUBS)
class TestGeometryStubs:
    def test_stub_parses_to_zero_rows_without_raising(self, name):
        """A geometry with no readings is information, not a parse failure."""
        data = read_dcip2d_data(EXPORT_DIR / name)
        assert data.records == []

    def test_stub_still_carries_its_header(self, name):
        data = read_dcip2d_data(EXPORT_DIR / name)
        assert data.array_type == "Pole-Dipole"
        assert "Line 3750 N" in data.title


# ---------------------------------------------------------------------------
# 2-D models
# ---------------------------------------------------------------------------

@needs_export
class TestModelMesh:
    @pytest.mark.parametrize("name", DC_MODELS + IP_MODELS)
    def test_every_model_is_one_1100_cell_55_by_20_mesh(self, name):
        model = read_dcip2d_model(EXPORT_DIR / name)
        assert (model.nx, model.nz) == (55, 20)
        assert model.values.shape == (20, 55)
        assert model.values.size == 1100
        assert model.air_mask.shape == model.values.shape
        assert model.air_mask.dtype == np.bool_

    @pytest.mark.parametrize("name", DC_MODELS + IP_MODELS)
    def test_every_model_has_the_same_98_air_cells(self, name):
        model = read_dcip2d_model(EXPORT_DIR / name)
        assert int(model.air_mask.sum()) == 98

    def test_air_mask_is_a_topographic_surface_row_major(self):
        """Row-major, surface first: air is an unbroken run down each column.

        0 to 3 cells thick, and 51 / 28 / 19 / 0 cells across the first four
        depth rows — a ground surface, thinning with depth and gone by row 3.
        """
        model = read_dcip2d_model(EXPORT_DIR / "ipinv2d.chg")
        runs = _air_run_lengths_from_top(model.air_mask)
        assert runs is not None, "an air cell sits beneath a rock cell"
        assert set(runs) == {0, 1, 2, 3}
        assert [int(row.sum()) for row in model.air_mask[:4]] == [51, 28, 19, 0]

    def test_column_major_reshape_is_not_a_topographic_surface(self):
        """The proof the order matters: the wrong reshape scatters the air.

        Same 98 cells, read down-column instead of across-row, land at all 20
        depths with isolated pockets under rock. Nothing raises — this is
        exactly the silent transpose the parser's docstring warns about.
        """
        model = read_dcip2d_model(EXPORT_DIR / "ipinv2d.chg")
        wrong = model.air_mask.flatten().reshape(model.nz, model.nx, order="F")
        assert int(wrong.sum()) == 98
        assert _air_run_lengths_from_top(wrong) is None

    def test_air_mask_is_identical_across_both_model_families(self):
        """One mesh, two inversions: dc and ip sections are co-registered.

        dcinv2d flags air by pinning conductivity to the array minimum;
        ipinv2d writes an explicit -1e30. Two unrelated conventions selecting
        the same 98 of 1,100 cells is the evidence, and it is what lets a
        chargeability section be overlaid on a resistivity section cell for
        cell without resampling.
        """
        conductivity = read_dcip2d_model(EXPORT_DIR / "dcinv2d.011")
        chargeability = read_dcip2d_model(EXPORT_DIR / "ipinv2d.chg")
        assert np.array_equal(conductivity.air_mask, chargeability.air_mask)

    @pytest.mark.parametrize("name", IP_MODELS)
    def test_ip_models_use_the_explicit_flag(self, name):
        model = read_dcip2d_model(EXPORT_DIR / name)
        assert (model.values[model.air_mask] == AIR_FLAG).all()

    @pytest.mark.parametrize(
        ("name", "pinned"),
        [("dcinv2d.011", 2.51485e-11), ("dcinv2d.016", 2.37633e-11), ("dcinv2d.030", 2.27286e-11)],
    )
    def test_dc_models_pin_air_to_a_value_that_moves_between_iterations(self, name, pinned):
        """Why the dc air rule cannot be a hard-coded sentinel.

        The pinned conductivity changes at every iteration of the SAME
        inversion. Matching on "is the repeated array minimum" survives that;
        matching on a literal would not.
        """
        model = read_dcip2d_model(EXPORT_DIR / name)
        assert (model.values[model.air_mask] == pinned).all()


@needs_export
class TestModelValues:
    def test_final_chargeability_range_and_median(self):
        model = read_dcip2d_model(EXPORT_DIR / "ipinv2d.chg")
        earth = model.values[~model.air_mask]
        assert earth.size == 1002
        assert earth.min() == 0.0
        assert earth.max() == 81.8788
        assert float(np.median(earth)) == pytest.approx(6.28597)

    def test_air_padding_would_wreck_the_range_if_left_in(self):
        """Why air_mask is returned at all rather than quietly dropped."""
        model = read_dcip2d_model(EXPORT_DIR / "ipinv2d.chg")
        assert model.values.min() == AIR_FLAG
        assert float(np.median(model.values)) != pytest.approx(6.28597)

    def test_final_conductivity_model_as_resistivity(self):
        """dcinv2d writes S/m; geologists read the section in ohm-m."""
        model = read_dcip2d_model(EXPORT_DIR / "dcinv2d.030")
        resistivity = 1.0 / model.values[~model.air_mask]
        assert resistivity.size == 1002
        assert float(resistivity.min()) == pytest.approx(81.58, abs=0.01)
        assert float(resistivity.max()) == pytest.approx(3381.73, abs=0.01)
        assert float(np.median(resistivity)) == pytest.approx(249.94, abs=0.01)


# ---------------------------------------------------------------------------
# Run manifest
# ---------------------------------------------------------------------------

@needs_export
class TestInpManifest:
    @pytest.fixture(scope="class")
    def manifest(self):
        return read_inp(EXPORT_DIR / "IP.inp")

    def test_ten_control_entries(self, manifest):
        assert len(manifest) == 10

    def test_labels_in_file_order(self, manifest):
        assert list(manifest) == [
            "niter, irest",
            "chifact",
            "obs file",
            "conductivity file",
            "mesh",
            "topography",
            "initial model",
            "reference model",
            "alpha's",
            "w.dat",
        ]

    def test_a_value_may_itself_contain_a_space(self, manifest):
        """Line 1 is the PAIR (niter, irest) — splitting on whitespace loses one."""
        assert manifest["niter, irest"] == "0 15"

    def test_windows_paths_are_kept_verbatim(self, manifest):
        """Provenance — where the run happened, not a path to follow."""
        assert manifest["obs file"] == (
            r"C:\Jobs\AES_AK_2005\CEN\IP\L3750N\export\CEN_L3750_Mx_XYZ.rdt"
        )
        assert manifest["conductivity file"] == "dcinv2d.con"
        assert manifest["mesh"] == "dcinv2d.msh"
        assert manifest["topography"] == (
            r"C:\Jobs\AES_AK_2005\CEN\IP\L3750N\export\L3750dz.txt"
        )

    def test_unsupplied_parameters_keep_their_null_token(self, manifest):
        assert INP_UNSET == "NULL"
        unset = [label for label, value in manifest.items() if value == INP_UNSET]
        assert unset == [
            "chifact",
            "initial model",
            "reference model",
            "alpha's",
            "w.dat",
        ]

    def test_the_manifest_names_the_mx_obs_file_that_was_not_delivered(self, manifest):
        """The run inverted CEN_L3750_Mx_XYZ.rdt; only its .rdtm* splits shipped.

        Recorded as a test because a caller resolving 'obs file' against the
        export directory will find nothing there, and should be told that is
        expected rather than treating it as a broken delivery.
        """
        obs = Path(manifest["obs file"].replace("\\", "/")).name
        assert obs == "CEN_L3750_Mx_XYZ.rdt"
        assert not (EXPORT_DIR / obs).exists()


# ---------------------------------------------------------------------------
# Malformed input — synthetic, because the delivery is clean
# ---------------------------------------------------------------------------

class TestMalformedInput:
    def test_data_file_shorter_than_its_header_raises(self, tmp_path):
        path = tmp_path / "truncated.rdt"
        path.write_text("Only a title line\n", encoding="ascii")
        with pytest.raises(ValueError, match="requires a title line"):
            read_dcip2d_data(path)

    def test_data_row_with_a_missing_electrode_raises(self, tmp_path):
        """A four-field row cannot be repaired by guessing which electrode went."""
        path = tmp_path / "short_row.rdt"
        path.write_text(
            "Title\nPole-Dipole\n"
            "  5494.88  5494.88  5442.79  5404.70  0.4126582\n"
            "  5494.88  5494.88  5404.70  0.2378481\n",
            encoding="ascii",
        )
        with pytest.raises(ValueError, match="line 4: expected 5 fields"):
            read_dcip2d_data(path)

    def test_model_cell_count_disagreeing_with_the_header_raises(self, tmp_path):
        """Padding or truncating shifts every later cell to the wrong place."""
        path = tmp_path / "short.001"
        path.write_text("   3   2\n 1.0 2.0 3.0\n 4.0 5.0\n", encoding="ascii")
        with pytest.raises(ValueError, match=r"6 cells\) but the file holds 5"):
            read_dcip2d_model(path)

    def test_model_without_an_integer_header_raises(self, tmp_path):
        path = tmp_path / "bad_header.001"
        path.write_text(" nx nz\n 1.0 2.0\n", encoding="ascii")
        with pytest.raises(ValueError, match="expected two integers"):
            read_dcip2d_model(path)

    def test_model_with_a_unique_minimum_gets_no_air_cells(self, tmp_path):
        """One spurious 'air' cell inside a section is worse than no mask."""
        path = tmp_path / "no_air.001"
        path.write_text("   2   2\n 1.0 2.0 3.0 4.0\n", encoding="ascii")
        model = read_dcip2d_model(path)
        assert not model.air_mask.any()

    def test_duplicate_control_label_raises_rather_than_overwriting(self, tmp_path):
        """Returning nine entries where the operator wrote ten is silent loss."""
        path = tmp_path / "dup.inp"
        path.write_text("a.obs  ! obs file\nb.obs  ! obs file\n", encoding="ascii")
        with pytest.raises(ValueError, match="duplicate label 'obs file'"):
            read_inp(path)

    def test_control_line_without_a_comment_is_kept_under_its_line_number(self, tmp_path):
        path = tmp_path / "bare.inp"
        path.write_text("0 15  ! niter, irest\ndcinv2d.msh\n", encoding="ascii")
        manifest = read_inp(path)
        assert manifest == {"niter, irest": "0 15", "line_2": "dcinv2d.msh"}

    def test_missing_file_raises_filenotfound(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            read_dcip2d_model(tmp_path / "absent.001")
